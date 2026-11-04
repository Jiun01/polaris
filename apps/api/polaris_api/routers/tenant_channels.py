"""Tenant Channels router — Phase 8 PR-5.

Mounted at ``/api/tenant/channels``.

One endpoint at GA:

  * ``GET /``  — list the calling tenant's connected channels for the
                 portal Channels screen. Read-only.

Channel CONNECT is intentionally NOT exposed here at GA. Per ADR-012
(sales-led motion) + ADR-034 Amendment 3rd (paste-only at GA), channel
provisioning happens via the platform_admin path:

  1. Customer sales conversation captures the WhatsApp System User token
     + phone_number_id + WABA ID (or Meta Page details for IG/Messenger).
  2. platform_admin runs the Chatwoot Platform API call to create the
     inbox, then writes the tenant_channels + tenant_secrets rows.
  3. Tenant logs in and sees the channel as connected.

A self-serve POST surface would short-circuit step (2) — but step (2)
requires Chatwoot inbox creation + tenant_secrets encryption with the
platform key, neither of which can be safely exposed to an unauthenticated
or weakly-authed tenant input. The "Coming in v3.0 — Embedded Signup"
flow per ADR-034 will land the POST shape when Tech Provider approval
unblocks the full self-serve path.

Auth: standard portal-JWT path via ``get_tenant_context``. RLS scopes
``tenant_channels`` reads to the calling tenant via app.current_tenant_id.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal, get_args
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-channels"])

_FORBID = ConfigDict(extra="forbid")

ChannelKind = Literal[
    "whatsapp", "instagram", "messenger", "telegram", "email", "instagram_login"
]


class TenantChannelDTO(BaseModel):
    """One row from tenant_channels exposed to the portal.

    Excludes the encrypted secret references (waba_id is fine as a
    public identifier; the system-user token + page access token live
    in tenant_secrets and are NEVER surfaced).
    """

    model_config = _FORBID

    id: UUID
    channel: ChannelKind
    status: str = Field(
        ...,
        description="active | needs_reconnect | suspended (per Phase 7 amendments)",
    )
    chatwoot_inbox_id: int
    waba_id: str | None = None
    phone_number_id: str | None = None
    fb_page_id: str | None = None
    ig_business_account_id: str | None = None
    wa_messaging_tier: str | None = None
    mm_lite_status: str | None = None
    telegram_bot_username: str | None = None
    created_at: str = Field(..., description="RFC3339 UTC")


class ChannelsListResponse(BaseModel):
    """GET / response envelope.

    Includes ``per_kind`` rollup so the portal tile can show 'WhatsApp
    (2 inboxes)' without re-grouping client-side.
    """

    model_config = _FORBID

    items: list[TenantChannelDTO]
    per_kind: dict[ChannelKind, int]


_LIST_SQL = """
SELECT id,
       channel,
       status,
       chatwoot_inbox_id,
       waba_id,
       phone_number_id,
       fb_page_id,
       ig_business_account_id,
       wa_messaging_tier,
       mm_lite_status,
       telegram_bot_username,
       created_at
  FROM tenant_channels
 WHERE tenant_id = $1
 ORDER BY created_at ASC
"""


@router.get(
    "",
    response_model=ChannelsListResponse,
    summary="List connected channels for the calling tenant",
    responses={
        401: {"description": "missing_bearer_token or invalid_token"},
        403: {"description": "service_suspended (payment_overdue only)"},
    },
)
async def list_channels(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> ChannelsListResponse:
    """Read-only list. Connect flow is admin-driven at GA (ADR-012)."""
    rows = await conn.fetch(_LIST_SQL, ctx.tenant_id)
    items: list[TenantChannelDTO] = []
    # Seed every ChannelKind to 0 so the rollup always has all keys AND a new
    # kind can never KeyError here (the prior hardcoded 4-key dict 500'd the
    # whole endpoint the moment an 'instagram_login' / 'email' row existed).
    per_kind: dict[ChannelKind, int] = dict.fromkeys(get_args(ChannelKind), 0)
    for r in rows:
        kind: ChannelKind = r["channel"]
        per_kind[kind] = per_kind.get(kind, 0) + 1
        created = r["created_at"]
        items.append(
            TenantChannelDTO(
                id=r["id"],
                channel=kind,
                status=r["status"],
                chatwoot_inbox_id=int(r["chatwoot_inbox_id"]),
                waba_id=r["waba_id"],
                phone_number_id=r["phone_number_id"],
                fb_page_id=r["fb_page_id"],
                ig_business_account_id=r["ig_business_account_id"],
                wa_messaging_tier=r["wa_messaging_tier"],
                mm_lite_status=r["mm_lite_status"],
                telegram_bot_username=r["telegram_bot_username"],
                created_at=(
                    created.isoformat() if isinstance(created, datetime) else str(created)
                ),
            )
        )
    return ChannelsListResponse(items=items, per_kind=per_kind)


class ChannelBranchUpdate(BaseModel):
    """Body for assigning (or clearing) an inbox's branch."""

    model_config = _FORBID

    tenant_branch_id: UUID | None = Field(
        default=None,
        description="Branch this inbox routes to; null clears the assignment.",
    )


class ChannelBranchResponse(BaseModel):
    model_config = _FORBID

    id: UUID
    tenant_branch_id: UUID | None


@router.patch(
    "/{channel_id}/branch",
    response_model=ChannelBranchResponse,
    summary="Assign (or clear) the branch an inbox routes to",
    responses={
        404: {"description": "channel_not_found or branch_not_found"},
    },
)
async def set_channel_branch(
    channel_id: UUID,
    body: ChannelBranchUpdate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> ChannelBranchResponse:
    """Write ``tenant_channels.tenant_branch_id`` — the missing link that lets
    branch-scoped handoff / EA routing / product+promo lookups actually fire
    (inbound conversations are stamped with this branch at creation). RLS scopes
    both reads/writes to the calling tenant; the FK is ``ON DELETE SET NULL``.
    """
    # When setting a branch, verify it belongs to the calling tenant (RLS-scoped)
    # so a valid-but-foreign branch id can't be attached via the FK layer.
    if body.tenant_branch_id is not None:
        branch_ok = await conn.fetchval(
            "SELECT 1 FROM tenant_branches WHERE id = $1", body.tenant_branch_id
        )
        if branch_ok is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="branch_not_found")

    updated = await conn.fetchval(
        """
        UPDATE tenant_channels
           SET tenant_branch_id = $2
         WHERE id = $1
        RETURNING id
        """,
        channel_id,
        body.tenant_branch_id,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel_not_found")
    logger.info(
        "tenant_channels.branch_set tenant=%s channel=%s branch=%s",
        ctx.tenant_id,
        channel_id,
        body.tenant_branch_id,
    )
    return ChannelBranchResponse(id=channel_id, tenant_branch_id=body.tenant_branch_id)
