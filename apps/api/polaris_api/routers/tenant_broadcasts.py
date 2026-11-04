"""Tenant-facing broadcasts API — Phase 6 PR-5.

Mounted at ``/api/tenant/broadcasts``. Eight endpoints covering the
broadcast authoring lifecycle. The worker that actually sends lands
in PR-6; here we ship the surface tenants use to create + schedule
+ pause + resume + cancel.

Endpoints:

  POST   /                    create draft broadcast (tier-gate Pro+)
  POST   /{id}/schedule       materialise recipients + ZADD
  POST   /{id}/pause          ZREM + status='paused'
  POST   /{id}/resume         status='paused' -> 'scheduled' + ZADD
  POST   /{id}/cancel         ZREM + status='cancelled' + recipients
                              pending -> skipped
  GET    /                    list (optional status filter)
  GET    /{id}                get one with counters
  GET    /{id}/recipients     paginated recipient list (optional
                              status filter)

Tier-gate (ADR-026 Amendment 3rd): broadcasts are Pro+ only;
Starter tenants get HTTP 403 with code='BROADCAST_TIER_BLOCKED'.

Error envelope codes (per 04g §5.12):

  BROADCAST_TIER_BLOCKED            tier-gate rejected (Pro+ only)
  BROADCAST_TEMPLATE_NOT_APPROVED   referenced template status != 'approved'
  BROADCAST_AUDIENCE_EMPTY          filter matched zero opted-in members
  BROADCAST_QUOTA_EXCEEDED          monthly_broadcast_recipients would
                                    be exceeded; transaction rolled back
  BROADCAST_STATE_INVALID           bad lifecycle transition
  BROADCAST_NOT_FOUND               id missing or RLS-stripped
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from polaris_shared_types import (
    BroadcastDTO,
    BroadcastRecipientDTO,
    CreateBroadcastRequest,
)

from polaris_api.deps.quota import enforce_quota
from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.broadcast import service as broadcast_service
from polaris_api.utils.errors import api_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-broadcasts"])


# ---------------------------------------------------------------------
# Row -> DTO projections
# ---------------------------------------------------------------------


def _row_to_broadcast_dto(row: asyncpg.Record) -> BroadcastDTO:
    """Project a broadcasts row into the wire DTO.

    audience_filter arrives from asyncpg as JSONB; Pydantic re-parses
    it via the discriminated-union — drift between the stored shape
    and the union will surface as ValidationError at construction
    time, which the router maps to a 500 (this would be a Phase 6
    bug, not a tenant-actionable one).
    """
    return BroadcastDTO(
        id=row["id"],
        tenant_id=row["tenant_id"],
        tenant_channel_id=row["tenant_channel_id"],
        template_id=row["template_id"],
        name=row["name"],
        status=row["status"],
        audience_filter=row["audience_filter"],
        variables_template=row["variables_template"],
        scheduled_at=row["scheduled_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        total_recipients=row["total_recipients"],
        sent_count=row["sent_count"],
        delivered_count=row["delivered_count"],
        read_count=row["read_count"],
        failed_count=row["failed_count"],
        skipped_count=row["skipped_count"],
        error=row["error"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_recipient_dto(row: asyncpg.Record) -> BroadcastRecipientDTO:
    return BroadcastRecipientDTO(
        id=row["id"],
        broadcast_id=row["broadcast_id"],
        audience_member_id=row["audience_member_id"],
        channel=row["channel"],
        channel_user_id=row["channel_user_id"],
        status=row["status"],
        wa_message_id=row["wa_message_id"],
        variables_resolved=row["variables_resolved"],
        skip_reason=row["skip_reason"],
        attempt_count=row["attempt_count"],
        sent_at=row["sent_at"],
        delivered_at=row["delivered_at"],
        read_at=row["read_at"],
        failed_at=row["failed_at"],
        failure_payload=row["failure_payload"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------
# Error -> envelope translation
# ---------------------------------------------------------------------


def _map_service_error(
    exc: broadcast_service.BroadcastError,
) -> HTTPException:
    """Translate a typed service error into an HTTPException with the
    documented envelope. Distinct subclasses get distinct status codes
    + envelope codes so the portal can render the right error UX."""
    if isinstance(exc, broadcast_service.BroadcastTierBlockedError):
        return api_error(
            "BROADCAST_TIER_BLOCKED",
            status.HTTP_403_FORBIDDEN,
            hint="Upgrade to Pro to send broadcasts",
        )
    if isinstance(exc, broadcast_service.BroadcastTemplateNotApprovedError):
        return api_error(
            "BROADCAST_TEMPLATE_NOT_APPROVED",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            message=str(exc),
        )
    if isinstance(exc, broadcast_service.BroadcastAudienceEmptyError):
        return api_error(
            "BROADCAST_AUDIENCE_EMPTY",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            message=str(exc),
        )
    if isinstance(exc, broadcast_service.BroadcastQuotaExceededError):
        return api_error(
            "BROADCAST_QUOTA_EXCEEDED",
            status.HTTP_429_TOO_MANY_REQUESTS,
            message=str(exc),
        )
    if isinstance(exc, broadcast_service.BroadcastStateInvalidError):
        return api_error(
            "BROADCAST_STATE_INVALID",
            status.HTTP_409_CONFLICT,
            message=str(exc),
        )
    # Generic BroadcastError (e.g. not-found inside service path).
    return api_error(
        "BROADCAST_NOT_FOUND",
        status.HTTP_404_NOT_FOUND,
        message=str(exc),
    )


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------


@router.post(
    "/",
    response_model=BroadcastDTO,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft broadcast (Pro+ tier-gated)",
)
async def create_broadcast(
    body: CreateBroadcastRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BroadcastDTO:
    try:
        new_id = await broadcast_service.create_broadcast(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            req=body,
        )
    except broadcast_service.BroadcastError as exc:
        raise _map_service_error(exc) from exc

    row = await _fetch_broadcast(conn, new_id)
    if row is None:
        # Shouldn't happen — we just inserted it. Defensive 500.
        raise api_error("BROADCAST_LOST_RACE", 500)
    return _row_to_broadcast_dto(row)


@router.post(
    "/{broadcast_id}/schedule",
    response_model=BroadcastDTO,
    summary="Materialise recipients + enqueue for worker drain",
)
async def schedule_broadcast(
    broadcast_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    _quota: Annotated[None, Depends(enforce_quota)],
) -> BroadcastDTO:
    try:
        await broadcast_service.schedule_broadcast(
            conn,
            tenant_id=ctx.tenant_id,
            broadcast_id=broadcast_id,
            actor_user_id=ctx.user_id,
        )
    except broadcast_service.BroadcastError as exc:
        raise _map_service_error(exc) from exc

    row = await _fetch_broadcast(conn, broadcast_id)
    if row is None:
        raise api_error("BROADCAST_NOT_FOUND", status.HTTP_404_NOT_FOUND)
    return _row_to_broadcast_dto(row)


@router.post(
    "/{broadcast_id}/pause",
    response_model=BroadcastDTO,
    summary="Pause a running or scheduled broadcast",
)
async def pause_broadcast(
    broadcast_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BroadcastDTO:
    try:
        await broadcast_service.pause_broadcast(
            conn,
            tenant_id=ctx.tenant_id,
            broadcast_id=broadcast_id,
            actor_user_id=ctx.user_id,
        )
    except broadcast_service.BroadcastError as exc:
        raise _map_service_error(exc) from exc

    row = await _fetch_broadcast(conn, broadcast_id)
    if row is None:
        raise api_error("BROADCAST_NOT_FOUND", status.HTTP_404_NOT_FOUND)
    return _row_to_broadcast_dto(row)


@router.post(
    "/{broadcast_id}/resume",
    response_model=BroadcastDTO,
    summary="Resume a paused broadcast",
)
async def resume_broadcast(
    broadcast_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BroadcastDTO:
    try:
        await broadcast_service.resume_broadcast(
            conn,
            tenant_id=ctx.tenant_id,
            broadcast_id=broadcast_id,
            actor_user_id=ctx.user_id,
        )
    except broadcast_service.BroadcastError as exc:
        raise _map_service_error(exc) from exc

    row = await _fetch_broadcast(conn, broadcast_id)
    if row is None:
        raise api_error("BROADCAST_NOT_FOUND", status.HTTP_404_NOT_FOUND)
    return _row_to_broadcast_dto(row)


@router.post(
    "/{broadcast_id}/cancel",
    response_model=BroadcastDTO,
    summary="Cancel a broadcast (pending recipients -> skipped)",
)
async def cancel_broadcast(
    broadcast_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BroadcastDTO:
    try:
        await broadcast_service.cancel_broadcast(
            conn,
            tenant_id=ctx.tenant_id,
            broadcast_id=broadcast_id,
            actor_user_id=ctx.user_id,
        )
    except broadcast_service.BroadcastError as exc:
        raise _map_service_error(exc) from exc

    row = await _fetch_broadcast(conn, broadcast_id)
    if row is None:
        raise api_error("BROADCAST_NOT_FOUND", status.HTTP_404_NOT_FOUND)
    return _row_to_broadcast_dto(row)


@router.get(
    "/",
    response_model=list[BroadcastDTO],
    summary="List broadcasts for the calling tenant",
)
async def list_broadcasts(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    status_filter: str | None = None,
) -> list[BroadcastDTO]:
    if status_filter is None:
        rows = await conn.fetch(_BROADCAST_SELECT_BASE + " ORDER BY created_at DESC")
    else:
        rows = await conn.fetch(
            _BROADCAST_SELECT_BASE + " WHERE status = $1 ORDER BY created_at DESC",
            status_filter,
        )
    return [_row_to_broadcast_dto(r) for r in rows]


@router.get(
    "/{broadcast_id}",
    response_model=BroadcastDTO,
    summary="Get one broadcast with counters",
)
async def get_broadcast(
    broadcast_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BroadcastDTO:
    row = await _fetch_broadcast(conn, broadcast_id)
    if row is None:
        raise api_error("BROADCAST_NOT_FOUND", status.HTTP_404_NOT_FOUND)
    return _row_to_broadcast_dto(row)


@router.get(
    "/{broadcast_id}/recipients",
    response_model=list[BroadcastRecipientDTO],
    summary="List recipients for a broadcast (optional status filter)",
)
async def list_recipients(
    broadcast_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    status_filter: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[BroadcastRecipientDTO]:
    if status_filter is None:
        rows = await conn.fetch(
            """
            SELECT id, broadcast_id, tenant_id, audience_member_id, channel,
                   channel_user_id, status, wa_message_id, variables_resolved,
                   skip_reason, attempt_count, sent_at, delivered_at, read_at,
                   failed_at, failure_payload, created_at, updated_at
              FROM broadcast_recipients
             WHERE broadcast_id = $1
             ORDER BY created_at ASC
             LIMIT $2 OFFSET $3
            """,
            broadcast_id,
            limit,
            offset,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, broadcast_id, tenant_id, audience_member_id, channel,
                   channel_user_id, status, wa_message_id, variables_resolved,
                   skip_reason, attempt_count, sent_at, delivered_at, read_at,
                   failed_at, failure_payload, created_at, updated_at
              FROM broadcast_recipients
             WHERE broadcast_id = $1 AND status = $2
             ORDER BY created_at ASC
             LIMIT $3 OFFSET $4
            """,
            broadcast_id,
            status_filter,
            limit,
            offset,
        )
    return [_row_to_recipient_dto(r) for r in rows]


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


_BROADCAST_SELECT_BASE = """
    SELECT id, tenant_id, tenant_channel_id, template_id, name, status,
           audience_filter, variables_template, scheduled_at, started_at,
           finished_at, total_recipients, sent_count, delivered_count,
           read_count, failed_count, skipped_count, error, created_by,
           created_at, updated_at
      FROM broadcasts
"""


async def _fetch_broadcast(
    conn: asyncpg.Connection, broadcast_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        _BROADCAST_SELECT_BASE + " WHERE id = $1",
        broadcast_id,
    )
