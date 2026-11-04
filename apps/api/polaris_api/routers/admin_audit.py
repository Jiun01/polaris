"""Admin audit-log viewer + chain verifier endpoints -- Phase 9 PR-5.

Mounted at /api/admin/audit.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from polaris_api.db.pools import get_service_pool
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin.pool import get_admin_service_conn
from polaris_api.services.audit_chain_verifier import run_verification

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-audit"])
_FORBID = ConfigDict(extra="forbid")

class _AuditLogItem(BaseModel):
    model_config = _FORBID
    id: UUID
    tenant_id: UUID
    actor: UUID | None
    action: str
    before: Any
    after: Any
    impersonating_kind: str | None
    impersonating_id: UUID | None
    created_at: str

class _AuditLogsResponse(BaseModel):
    model_config = _FORBID
    items: list[_AuditLogItem]
    next_cursor: UUID | None

class _VerificationResponse(BaseModel):
    model_config = _FORBID
    id: UUID
    checked_at: str
    status: str
    rows_scanned: int
    duration_ms: int
    broken_at_id: UUID | None
    broken_at_created_at: str | None
    broken_prev_hash_hex: str | None
    broken_actual_hash_hex: str | None
    triggered_by: str
    triggered_by_admin_id: UUID | None
    notes: str | None

class _VerifyTriggerResponse(BaseModel):
    model_config = _FORBID
    status: str
    message: str

def _row_to_audit_item(row: asyncpg.Record) -> _AuditLogItem:
    return _AuditLogItem(
        id=row["id"],
        tenant_id=row["tenant_id"],
        actor=row["actor"],
        action=row["action"],
        before=json.loads(row["before"]) if isinstance(row["before"], str) else row["before"],
        after=json.loads(row["after"]) if isinstance(row["after"], str) else row["after"],
        impersonating_kind=row["impersonating_kind"],
        impersonating_id=row["impersonating_id"],
        created_at=row["created_at"].astimezone(UTC).isoformat(),
    )

def _row_to_verification(row: asyncpg.Record) -> _VerificationResponse:
    return _VerificationResponse(
        id=row["id"],
        checked_at=row["checked_at"].astimezone(UTC).isoformat(),
        status=row["status"],
        rows_scanned=row["rows_scanned"],
        duration_ms=row["duration_ms"],
        broken_at_id=row["broken_at_id"],
        broken_at_created_at=(
            row["broken_at_created_at"].astimezone(UTC).isoformat()
            if row["broken_at_created_at"] is not None
            else None
        ),
        broken_prev_hash_hex=(
            bytes(row["broken_prev_hash"]).hex()
            if row["broken_prev_hash"] is not None
            else None
        ),
        broken_actual_hash_hex=(
            bytes(row["broken_actual_hash"]).hex()
            if row["broken_actual_hash"] is not None
            else None
        ),
        triggered_by=row["triggered_by"],
        triggered_by_admin_id=row["triggered_by_admin_id"],
        notes=row["notes"],
    )

@router.get("/logs", response_model=_AuditLogsResponse)
async def list_audit_logs(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
    tenant_id: Annotated[UUID | None, Query()] = None,
    actor_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[UUID | None, Query(description="Last seen id")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> _AuditLogsResponse:
    """Filterable cursor-paginated audit log viewer."""
    conditions: list[str] = []
    args: list[Any] = []
    idx = 1
    if tenant_id is not None:
        conditions.append("al.tenant_id = $" + str(idx))
        args.append(tenant_id)
        idx += 1
    if actor_id is not None:
        conditions.append("al.actor = $" + str(idx))
        args.append(actor_id)
        idx += 1
    if action is not None:
        conditions.append("al.action = $" + str(idx))
        args.append(action)
        idx += 1
    if date_from is not None:
        conditions.append("al.created_at >= $" + str(idx))
        args.append(date_from)
        idx += 1
    if date_to is not None:
        conditions.append("al.created_at <= $" + str(idx))
        args.append(date_to)
        idx += 1
    if cursor is not None:
        cursor_row = await conn.fetchrow(
            "SELECT created_at, id FROM audit_logs WHERE id = $1", cursor
        )
        if cursor_row is None:
            raise HTTPException(status_code=400, detail="invalid_cursor")
        conditions.append(
            "(al.created_at, al.id) < ($" + str(idx) + ", $" + str(idx + 1) + ")"
        )
        args.append(cursor_row["created_at"])
        args.append(cursor_row["id"])
        idx += 2
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    args.append(limit + 1)
    query = (
        "SELECT al.id, al.tenant_id, al.actor, al.action,"  # noqa: S608
        " al.before, al.after, al.impersonating_kind, al.impersonating_id,"
        " al.created_at FROM audit_logs al "
        + where_clause
        + " ORDER BY al.created_at DESC, al.id DESC LIMIT $" + str(idx)
    )
    rows = await conn.fetch(query, *args)  # noqa: S608
    has_more = len(rows) > limit
    page = rows[:limit]
    nxt: UUID | None = page[-1]["id"] if has_more and page else None
    return _AuditLogsResponse(items=[_row_to_audit_item(r) for r in page], next_cursor=nxt)

@router.get("/verifications/latest", response_model=_VerificationResponse)
async def get_latest_verification(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _VerificationResponse:
    """Return the most recent audit_chain_verifications row."""
    row = await conn.fetchrow(
        "SELECT id, checked_at, status, rows_scanned, duration_ms,"
        " broken_at_id, broken_at_created_at, broken_prev_hash, broken_actual_hash,"
        " triggered_by, triggered_by_admin_id, notes"
        " FROM audit_chain_verifications ORDER BY checked_at DESC LIMIT 1"
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no_verification_run_yet")
    return _row_to_verification(row)


@router.post("/verify", response_model=_VerifyTriggerResponse)
async def trigger_verification(
    background_tasks: BackgroundTasks,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _VerifyTriggerResponse:
    """Trigger on-demand audit chain re-verification in the background."""
    pool = get_service_pool()

    async def _run() -> None:
        await run_verification(
            triggered_by="on_demand",
            triggered_by_admin_id=admin_id,
            pool=pool,
        )

    background_tasks.add_task(_run)
    return _VerifyTriggerResponse(
        status="triggered",
        message=(
            "Audit chain re-verification started. "
            "Poll GET /api/admin/audit/verifications/latest for result."
        ),
    )
