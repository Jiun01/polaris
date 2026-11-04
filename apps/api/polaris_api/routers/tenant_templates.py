"""Tenant-facing template authoring API — Phase 6 PR-3.

Mounted at ``/api/tenant/templates`` per the standard prefix layering
(``/v1/webhooks/*`` + ``/api/tenant/*`` + ``/api/admin/*``).

Three endpoints:

  * ``GET /``                 — list templates for the calling tenant
  * ``POST /``                — submit a new template to Meta + persist
                                local row with status='pending'
  * ``DELETE /{template_id}`` — best-effort Meta delete + soft-disable
                                local row to status='disabled'

Every endpoint runs through ``get_tenant_context`` so RLS scoping is
identical to the Phase 3/5 routes. Template ids that don't belong to
the calling tenant 404 (RLS strips them).

Error envelope codes (per 04g §5.9):

  * ``TEMPLATE_NOT_FOUND``           — bad template_id or wrong tenant
  * ``TEMPLATE_CHANNEL_NOT_FOUND``   — tenant_channel_id missing or
                                       wrong tenant
  * ``TEMPLATE_CHANNEL_NOT_WHATSAPP``— channel binding isn't WhatsApp
  * ``TEMPLATE_WABA_NOT_CONFIGURED`` — channel row missing waba_id
                                       (paste-only onboarding pending)
  * ``TEMPLATE_SECRET_MISSING``      — tenant_secrets has no
                                       waba_access_token for this tenant
  * ``TEMPLATE_META_ERROR``          — Meta rejected the submit; the
                                       error envelope from MetaTemplateError
                                       is returned as details
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from polaris_shared_types import TenantTemplateDTO, TenantTemplateSubmit

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services import audit, secrets
from polaris_api.services.meta_whatsapp import (
    MetaTemplateError,
    get_meta_whatsapp_client,
)
from polaris_api.services.templates import repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-templates"])

# Kind we persist under in tenant_secrets for the WABA System User token.
# Matches the channel-onboarding flow's persistence call (Phase 8).
_WABA_TOKEN_KIND = "meta_waba_access_token"  # noqa: S105 — secret KIND label, not a secret value


def _row_to_dto(row: asyncpg.Record) -> TenantTemplateDTO:
    """Project a tenant_templates row into the wire DTO.

    components arrives from asyncpg as a JSONB-decoded list[dict]; we
    pass it through verbatim — the Pydantic discriminated union on
    TemplateComponent.type validates each entry at construction time.
    """
    return TenantTemplateDTO(
        id=row["id"],
        tenant_id=row["tenant_id"],
        tenant_channel_id=row["tenant_channel_id"],
        name=row["name"],
        language=row["language"],
        category=row["category"],
        status=row["status"],
        meta_template_id=row["meta_template_id"],
        components=row["components"],
        rejection_reason=row["rejection_reason"],
        last_status_change_at=row["last_status_change_at"],
        submitted_at=row["submitted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get(
    "/",
    response_model=list[TenantTemplateDTO],
    summary="List templates for the calling tenant",
)
async def list_templates(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    status_filter: str | None = None,
) -> list[TenantTemplateDTO]:
    """Returns every template visible under RLS, ordered newest-first.

    Optional ``?status_filter=approved`` narrows to one lifecycle
    state — the common portal use case ("show me sendable templates"
    for the broadcast composer).
    """
    rows = await repo.list_templates(conn, status=status_filter)
    return [_row_to_dto(row) for row in rows]


class _SubmitBody(TenantTemplateSubmit):
    """Request body extends the shared TenantTemplateSubmit with the
    tenant_channel_id binding (which WhatsApp inbox the template
    submits under).
    """

    tenant_channel_id: UUID


@router.post(
    "/",
    response_model=TenantTemplateDTO,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a new template to Meta + persist as pending",
)
async def submit_template(
    body: _SubmitBody,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantTemplateDTO:
    """Submit + persist in a single transaction.

    Steps:
      1. Resolve tenant_channel under RLS — must exist + be WhatsApp
         + carry a waba_id from channel onboarding.
      2. Decrypt the per-tenant WABA System User token from
         tenant_secrets (per ADR-034 paste-only).
      3. Call meta_whatsapp.submit_template; persist the returned
         meta_template_id alongside the local row.
      4. Audit the submission.

    Meta-side failure (MetaTemplateError) surfaces as 422 with the
    structured envelope — the portal renders Meta's error_data.details
    directly to the tenant.
    """
    channel = await conn.fetchrow(
        """
        SELECT id, channel, waba_id
          FROM tenant_channels
         WHERE id = $1
        """,
        body.tenant_channel_id,
    )
    if channel is None:
        # RLS-stripped: tenant doesn't own this channel id.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TEMPLATE_CHANNEL_NOT_FOUND"},
        )
    if channel["channel"] != "whatsapp":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "TEMPLATE_CHANNEL_NOT_WHATSAPP"},
        )
    if not channel["waba_id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "TEMPLATE_WABA_NOT_CONFIGURED"},
        )

    token_bytes = await secrets.get(conn, ctx.tenant_id, _WABA_TOKEN_KIND)
    if token_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "TEMPLATE_SECRET_MISSING"},
        )
    access_token = token_bytes.decode("utf-8")

    components_payload: list[dict[str, Any]] = [
        comp.model_dump(exclude_none=True) for comp in body.components
    ]

    client = get_meta_whatsapp_client()
    try:
        meta_template_id = await client.submit_template(
            waba_id=channel["waba_id"],
            name=body.name,
            language=body.language,
            category=body.category,
            components=components_payload,
            access_token=access_token,
        )
    except MetaTemplateError as exc:
        logger.warning(
            "submit_template: Meta rejected tenant=%s name=%s code=%s "
            "subcode=%s details=%s",
            ctx.tenant_id,
            body.name,
            exc.code,
            exc.error_subcode,
            exc.details,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "TEMPLATE_META_ERROR",
                "meta_code": exc.code,
                "meta_error_subcode": exc.error_subcode,
                "meta_details": exc.details,
                "meta_status_code": exc.status_code,
            },
        ) from exc

    template_id = await repo.create_template(
        conn,
        tenant_id=ctx.tenant_id,
        tenant_channel_id=body.tenant_channel_id,
        name=body.name,
        language=body.language,
        category=body.category,
        components=components_payload,
        meta_template_id=meta_template_id,
    )

    await audit.record(
        conn,
        ctx.tenant_id,
        ctx.user_id,
        "template.submitted",
        before=None,
        after={
            "template_id": str(template_id),
            "name": body.name,
            "language": body.language,
            "category": body.category,
            "meta_template_id": meta_template_id,
        },
    )

    row = await repo.get_template(conn, template_id)
    assert row is not None  # noqa: S101 — we just inserted it
    return _row_to_dto(row)


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a template (Meta + local)",
)
async def delete_template(
    template_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> None:
    """Best-effort Meta delete + flip local row to status='disabled'.

    Meta accepts deletes on any status (incl. PENDING which would
    normally 400 on portal-side workflows); we let Meta decide and
    catch the error envelope for surface-able failures.

    Local row is soft-set to disabled rather than DROP'd so audit /
    broadcast history that references it stays resolvable.
    """
    row = await repo.get_template(conn, template_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TEMPLATE_NOT_FOUND"},
        )

    # If we never got a meta_template_id (timed-out submit), skip the
    # Meta-side delete — there's nothing to delete remotely yet.
    if row["meta_template_id"]:
        channel = await conn.fetchrow(
            "SELECT waba_id FROM tenant_channels WHERE id = $1",
            row["tenant_channel_id"],
        )
        token_bytes = await secrets.get(conn, ctx.tenant_id, _WABA_TOKEN_KIND)
        if channel and channel["waba_id"] and token_bytes is not None:
            client = get_meta_whatsapp_client()
            try:
                await client.delete_template(
                    waba_id=channel["waba_id"],
                    name=row["name"],
                    access_token=token_bytes.decode("utf-8"),
                )
            except MetaTemplateError as exc:
                # Non-404 Meta error: log + still proceed with the
                # local soft-disable. Tenants who want hard cleanup
                # can resubmit + redelete; the local-side flip is
                # what the portal UX actually observes.
                logger.warning(
                    "delete_template: Meta delete failed tenant=%s name=%s "
                    "code=%s — proceeding with local soft-disable",
                    ctx.tenant_id,
                    row["name"],
                    exc.code,
                )

    await repo.update_status(
        conn,
        template_id=template_id,
        status="disabled",
        rejection_reason=row["rejection_reason"],
    )

    await audit.record(
        conn,
        ctx.tenant_id,
        ctx.user_id,
        "template.deleted",
        before={"status": row["status"]},
        after={"status": "disabled"},
    )
