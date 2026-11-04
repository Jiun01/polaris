"""Tenant-facing DSAR (Data Subject Access Request) endpoints — Phase 10 PR-3.

Mounted at ``/api/tenant/privacy``. Three endpoints covering GDPR/PDPA
data-subject rights:

  POST   /export                    Submit a DSAR data-export request.
                                    Creates a ``dsar_export_requests`` row
                                    with ``status='pending'``.  A background
                                    worker (Phase 11) picks it up and uploads
                                    a signed-URL bundle within 24 h SLA.
                                    Idempotent: a second call within the
                                    same calendar day returns the existing
                                    pending/processing row rather than
                                    creating a duplicate.

  GET    /export/status/{id}        Poll a previously submitted export
                                    request.  Returns current status +
                                    ``signed_url`` when ``status='ready'``.

  POST   /delete                    Tenant self-service account-deletion
                                    request.  Flips ``tenants.status`` to
                                    ``'deleted'`` and starts the 30-day DSAR
                                    grace window (ADR-029).  Physical cascade
                                    purge runs in the nightly retention
                                    sweeper once the grace window expires.

Auth: standard tenant JWT (``get_tenant_context``).  The ``polaris_anon``
connection enforces RLS so tenants can only see their own export rows.
Service-role calls are made via ``get_request_conn`` which the dep injects
in the same RLS-scoped transaction.

Error codes:
  EXPORT_ALREADY_PENDING   a pending/processing export exists today;
                           ``request_id`` of the existing row is returned.
  EXPORT_NOT_FOUND         no export row with that id for this tenant.
  TENANT_ALREADY_DELETED   tenant is already in soft-delete state.

ADR refs: ADR-029 (30-day grace), ADR-027 (demo data in DSAR window).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.audit import record as audit_record

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-privacy"])


# ── Response shapes (inline — DSAR not yet in shared-types TS surface) ────────


class DSARExportResponse(BaseModel):
    """Minimal response for export-request creation / idempotency return."""

    request_id: UUID
    status: str
    message: str


class DSARExportStatusResponse(BaseModel):
    """Full status payload returned by the polling endpoint."""

    request_id: UUID
    status: str  # pending | processing | ready | expired | failed
    signed_url: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    completed_at: datetime | None = None


class DSARDeleteResponse(BaseModel):
    """Confirmation envelope for the self-service account-deletion endpoint."""

    ok: bool
    message: str
    grace_expires_at: datetime


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/export",
    response_model=DSARExportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a DSAR data-export request",
)
async def request_export(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> DSARExportResponse:
    """Submit a DSAR export.

    Idempotency guard: if a ``pending`` or ``processing`` export already
    exists for this tenant (created within the last 24 h to avoid stale
    matches), return it instead of creating a second row.  This lets portal
    UIs hit the button repeatedly without creating runaway jobs.
    """
    window_start = datetime.now(UTC) - timedelta(hours=24)
    existing = await conn.fetchrow(
        """
        SELECT id, status, created_at
          FROM dsar_export_requests
         WHERE tenant_id = $1
           AND status IN ('pending', 'processing')
           AND created_at >= $2
         ORDER BY created_at DESC
         LIMIT 1
        """,
        ctx.tenant_id,
        window_start,
    )
    if existing is not None:
        logger.info(
            "privacy.export: idempotent return tenant=%s request_id=%s status=%s",
            ctx.tenant_id,
            existing["id"],
            existing["status"],
        )
        return DSARExportResponse(
            request_id=existing["id"],
            status=existing["status"],
            message="An export request is already in progress.",
        )

    row = await conn.fetchrow(
        """
        INSERT INTO dsar_export_requests (tenant_id, requested_by)
        VALUES ($1, $2)
        RETURNING id, status
        """,
        ctx.tenant_id,
        ctx.user_id,
    )
    assert row is not None  # INSERT can't return None

    await audit_record(
        conn,
        tenant_id=ctx.tenant_id,
        actor=ctx.user_id,
        action="privacy.export_requested",
        after={"request_id": str(row["id"])},
    )

    logger.info(
        "privacy.export: created request_id=%s tenant=%s",
        row["id"],
        ctx.tenant_id,
    )
    return DSARExportResponse(
        request_id=row["id"],
        status=row["status"],
        message=(
            "Your data-export request has been received. "
            "You will receive a download link within 24 hours."
        ),
    )


@router.get(
    "/export/status/{request_id}",
    response_model=DSARExportStatusResponse,
    summary="Poll a DSAR export request",
)
async def get_export_status(
    request_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> DSARExportStatusResponse:
    """Return current status + ``signed_url`` when ready.

    RLS on ``dsar_export_requests`` ensures tenants can only see their own
    rows, so the 404 branch covers both "not found" and "belongs to another
    tenant" without leaking existence information.
    """
    row = await conn.fetchrow(
        """
        SELECT id, status, signed_url, expires_at, created_at, completed_at
          FROM dsar_export_requests
         WHERE id = $1
           AND tenant_id = $2
        """,
        request_id,
        ctx.tenant_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "EXPORT_NOT_FOUND",
                "message": f"export request {request_id} not found",
            },
        )
    return DSARExportStatusResponse(
        request_id=row["id"],
        status=row["status"],
        signed_url=row["signed_url"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


@router.post(
    "/delete",
    response_model=DSARDeleteResponse,
    summary="Request self-service account deletion (30-day grace per ADR-029)",
)
async def request_deletion(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> DSARDeleteResponse:
    """Soft-delete the calling tenant's account.

    Flips ``tenants.status`` → ``'deleted'`` which starts the 30-day DSAR
    grace window.  The nightly retention sweeper physically cascades the
    delete once ``updated_at + 30 days`` passes (ADR-029).

    The tenant JWT remains valid until the grace expires; calls to protected
    endpoints on a ``deleted`` tenant return ``403 service_suspended`` from
    ``get_tenant_context``.

    Idempotent: if the tenant is already ``deleted``, the existing
    ``updated_at`` (= the original delete timestamp) is preserved so the
    grace clock isn't reset by repeated calls.
    """
    async with conn.transaction():
        tenant_row = await conn.fetchrow(
            "SELECT status, updated_at FROM tenants WHERE id = $1 FOR UPDATE",
            ctx.tenant_id,
        )
        if tenant_row is None:
            # Shouldn't happen — JWT is scoped to an existing tenant.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "TENANT_NOT_FOUND"},
            )

        if tenant_row["status"] == "deleted":
            # Already deleted — return idempotent response using original timestamp.
            updated_utc = (
                tenant_row["updated_at"]
                if tenant_row["updated_at"].tzinfo is not None
                else tenant_row["updated_at"].replace(tzinfo=UTC)
            )
            grace_expires_at = updated_utc + timedelta(days=30)
            return DSARDeleteResponse(
                ok=True,
                message="Account deletion is already in progress.",
                grace_expires_at=grace_expires_at,
            )

        now_utc = datetime.now(UTC)
        await conn.execute(
            """
            UPDATE tenants
               SET status             = 'deleted',
                   suspended_reason   = 'dsar_self_service',
                   updated_at         = $2
             WHERE id = $1
            """,
            ctx.tenant_id,
            now_utc,
        )

        await audit_record(
            conn,
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="privacy.delete_requested",
            before={"status": tenant_row["status"]},
            after={
                "status": "deleted",
                "reason": "dsar_self_service",
                "grace_expires_at": (now_utc + timedelta(days=30)).isoformat(),
            },
        )

    grace_expires_at = now_utc + timedelta(days=30)
    logger.info(
        "privacy.delete: tenant=%s soft-deleted, grace_expires=%s",
        ctx.tenant_id,
        grace_expires_at.isoformat(),
    )
    return DSARDeleteResponse(
        ok=True,
        message=(
            "Your account deletion request has been submitted. "
            "All data will be permanently removed after the 30-day grace period."
        ),
        grace_expires_at=grace_expires_at,
    )
