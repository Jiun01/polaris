"""Tenant-facing branch CRUD — Phase 8 PR-9.

Mounted at ``/api/tenant/branches``. Surfaces the Pro+ multi-branch
product per ADR-041 Amendment. Tier-gating is enforced both UI-side
via <TierGate> and backend via ``tenant_quotas.multi_branch_enabled``
(Phase 10 PR-1 — ADR-026).  A Starter tenant who bypasses the UI
gate receives HTTP 403 MULTI_BRANCH_TIER_BLOCKED.

Four endpoints (no PATCH separately — UPDATE is full-shape via PUT-
style PATCH; matches the promotions router):

  * ``GET    /``       — list active branches.
  * ``POST   /``       — create new branch.
  * ``PATCH  /{id}``   — rename.
  * ``DELETE /{id}``   — delete (cascades NULL onto audience_members /
                         knowledge_chunks / ingestion_requests via
                         ON DELETE SET NULL declared at FK-add time).

Uniqueness: ``(tenant_id, name)`` is UNIQUE per migration 017a so
collisions surface as ``BRANCH_NAME_TAKEN`` 409s.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, status
from polaris_shared_types import TenantBranch, TenantBranchUpsert

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.utils.errors import api_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-branches"])


_BRANCH_COLS = """id, tenant_id, name, slug, business_hours,
       handoff_message_in_hours, handoff_message_after_hours,
       ea_provider_id, ea_default_service_id, created_at"""


def _row_to_dto(row: asyncpg.Record) -> TenantBranch:
    bh = row["business_hours"]
    return TenantBranch(
        id=row["id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        slug=row["slug"],
        # asyncpg returns JSONB as a str unless a codec is registered; normalise.
        business_hours=json.loads(bh) if isinstance(bh, str) else bh,
        handoff_message_in_hours=row["handoff_message_in_hours"],
        handoff_message_after_hours=row["handoff_message_after_hours"],
        ea_provider_id=row["ea_provider_id"],
        ea_default_service_id=row["ea_default_service_id"],
        created_at=row["created_at"],
    )


@router.get(
    "/",
    response_model=list[TenantBranch],
    summary="List the tenant's branches",
)
async def list_branches(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> list[TenantBranch]:
    rows = await conn.fetch(
        f"""
        SELECT {_BRANCH_COLS}
        FROM tenant_branches
        ORDER BY created_at ASC
        """  # noqa: S608 — _BRANCH_COLS is a static constant, no interpolation of input
    )
    return [_row_to_dto(r) for r in rows]


@router.post(
    "/",
    response_model=TenantBranch,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new branch",
)
async def create_branch(
    body: TenantBranchUpsert,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantBranch:
    # Phase 10 PR-1 — backend tier gate (ADR-026).
    # UI <TierGate> blocks Starter before the request arrives, but we
    # enforce server-side too so a spoofed JWT can't bypass the gate.
    multi_branch_ok: bool = (
        await conn.fetchval(
            "SELECT multi_branch_enabled FROM tenant_quotas WHERE tenant_id = $1",
            ctx.tenant_id,
        )
        or False
    )
    if not multi_branch_ok:
        raise api_error(
            "MULTI_BRANCH_TIER_BLOCKED",
            status.HTTP_403_FORBIDDEN,
            hint="Upgrade to Pro to create multiple branches",
        )
    try:
        row = await conn.fetchrow(
            f"""
            INSERT INTO tenant_branches
                (tenant_id, name, business_hours, handoff_message_in_hours,
                 handoff_message_after_hours, ea_provider_id, ea_default_service_id)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7)
            RETURNING {_BRANCH_COLS}
            """,  # noqa: S608 — _BRANCH_COLS is a static constant
            ctx.tenant_id,
            body.name,
            json.dumps(body.business_hours) if body.business_hours is not None else None,
            body.handoff_message_in_hours,
            body.handoff_message_after_hours,
            body.ea_provider_id,
            body.ea_default_service_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise api_error(
            "BRANCH_NAME_TAKEN",
            status.HTTP_409_CONFLICT,
            message=f"a branch named {body.name!r} already exists",
        ) from exc
    assert row is not None
    logger.info("branch.create tenant=%s id=%s name=%r",
                ctx.tenant_id, row["id"], body.name)
    return _row_to_dto(row)


@router.patch(
    "/{branch_id}",
    response_model=TenantBranch,
    summary="Update a branch (name + hours + handoff messages + EA routing)",
)
async def update_branch(
    branch_id: UUID,
    body: TenantBranchUpsert,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantBranch:
    # COALESCE on the optional fields: a null/omitted field leaves the stored
    # value untouched, so editing hours never NULLs out the ea_* ids that branch
    # provisioning (services/booking/branch_bindings.py) wrote.
    try:
        row = await conn.fetchrow(
            f"""
            UPDATE tenant_branches
            SET name = $1,
                business_hours = COALESCE($3::jsonb, business_hours),
                handoff_message_in_hours = COALESCE($4, handoff_message_in_hours),
                handoff_message_after_hours = COALESCE($5, handoff_message_after_hours),
                ea_provider_id = COALESCE($6, ea_provider_id),
                ea_default_service_id = COALESCE($7, ea_default_service_id)
            WHERE id = $2
            RETURNING {_BRANCH_COLS}
            """,  # noqa: S608 — _BRANCH_COLS is a static constant
            body.name,
            branch_id,
            json.dumps(body.business_hours) if body.business_hours is not None else None,
            body.handoff_message_in_hours,
            body.handoff_message_after_hours,
            body.ea_provider_id,
            body.ea_default_service_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise api_error(
            "BRANCH_NAME_TAKEN",
            status.HTTP_409_CONFLICT,
            message=f"another branch already uses name {body.name!r}",
        ) from exc
    if row is None:
        raise api_error(
            "BRANCH_NOT_FOUND",
            status.HTTP_404_NOT_FOUND,
            message=f"branch {branch_id} not found",
        )
    return _row_to_dto(row)


@router.delete(
    "/{branch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a branch",
)
async def delete_branch(
    branch_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> None:
    """Delete a branch. The FKs on knowledge_chunks /
    ingestion_requests / audience_members declare ON DELETE SET NULL
    (per ADR-041 — branch deletion shouldn't cascade-delete content),
    so rows scoped to this branch simply become tenant-wide after the
    delete."""
    result = await conn.execute(
        "DELETE FROM tenant_branches WHERE id = $1", branch_id,
    )
    if result.endswith(" 0"):
        raise api_error(
            "BRANCH_NOT_FOUND",
            status.HTTP_404_NOT_FOUND,
            message=f"branch {branch_id} not found",
        )
    logger.info("branch.delete tenant=%s id=%s", ctx.tenant_id, branch_id)
    return None
