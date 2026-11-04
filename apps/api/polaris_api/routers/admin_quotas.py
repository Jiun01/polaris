"""Quota management admin routes — Phase 9 PR-4.

Mounted at /api/admin/quotas.

GET  /usage                  - all-tenants usage table
PUT  /{tenant_id}/override   - per-tenant quota column override

Auth: require_platform_admin. JWT role=platform_admin, no tenant_id.
16 overridable columns from migration 007_tenant_quotas.sql.
Any other column name returns 422.

ADR-017: quota dimensions locked. ADR-030: audit every write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from polaris_api.routers.admin_tenants import (
    _CANONICAL_CHANNEL_KINDS,
    require_platform_admin,
)
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-quotas"])

_FORBID = ConfigDict(extra="forbid")

_QUOTA_COLUMNS: frozenset[str] = frozenset({
    "bot_replies", "vision_calls_per_month", "conversation_retention_days",
    "allowed_channel_kinds", "outbound_broadcast_enabled", "multi_branch_enabled",
    "knowledge_chunks_total_mb", "rate_limit_per_minute", "monthly_llm_tokens_in",
    "monthly_llm_tokens_out", "monthly_ingestion_pages", "monthly_broadcast_recipients",
    "monthly_bookings", "agent_seats_included", "agent_seats_extra",
    "bot_replies_period_cap",
})

_BOOLEAN_COLUMNS: frozenset[str] = frozenset({
    "outbound_broadcast_enabled", "multi_branch_enabled",
})



class _QuotaUsageRow(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    tenant_name: str
    plan_tier: str | None
    bot_replies: int
    bot_replies_period_cap: int
    vision_calls_per_month: int
    conversation_retention_days: int
    knowledge_chunks_total_mb: int
    rate_limit_per_minute: int
    monthly_llm_tokens_in: int
    monthly_llm_tokens_out: int
    monthly_ingestion_pages: int
    monthly_broadcast_recipients: int
    monthly_bookings: int
    agent_seats_included: int
    agent_seats_extra: int
    outbound_broadcast_enabled: bool
    multi_branch_enabled: bool
    allowed_channel_kinds: list[str]
    usage_bot_replies_this_period: int
    usage_pct: float


class _QuotaUsageResponse(BaseModel):
    model_config = _FORBID
    items: list[_QuotaUsageRow]
    total: int


class _OverrideRequest(BaseModel):
    model_config = _FORBID
    column: str
    # int for the numeric/boolean quota columns; list[str] for the one array
    # column (allowed_channel_kinds); None resets a numeric column to 0.
    value: int | list[str] | None


class _OverrideResponse(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    column: str
    new_value: int | list[str] | None
    overridden_at: str


def _safe_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _coerce_audit_val(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return list(v)
    return v


@router.get("/usage", response_model=_QuotaUsageResponse)
async def get_quota_usage(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _QuotaUsageResponse:
    """All-tenants usage table.

    Returns tenant_quotas caps plus bot-reply usage read from the SAME
    authoritative counter the tenant dashboard / billing / health pages use
    (``tenant_quotas.bot_replies_used_this_period``, flushed from the Redis
    cap counter). Previously this approximated usage by counting every
    assistant message, which over-counted handoff/holding turns and disagreed
    with the tenant-facing number (QA finding L2 — admin showed 22 vs 15).
    """
    rows = await conn.fetch(
        """
        SELECT
          t.id                          AS tenant_id,
          t.display_name                AS tenant_name,
          tq.plan_tier,
          tq.bot_replies,
          tq.bot_replies_period_cap,
          tq.vision_calls_per_month,
          tq.conversation_retention_days,
          tq.knowledge_chunks_total_mb,
          tq.rate_limit_per_minute,
          tq.monthly_llm_tokens_in,
          tq.monthly_llm_tokens_out,
          tq.monthly_ingestion_pages,
          tq.monthly_broadcast_recipients,
          tq.monthly_bookings,
          tq.agent_seats_included,
          tq.agent_seats_extra,
          tq.outbound_broadcast_enabled,
          tq.multi_branch_enabled,
          tq.allowed_channel_kinds,
          COALESCE(tq.bot_replies_used_this_period, 0) AS usage_bot_replies_this_period
        FROM tenants t
        JOIN tenant_quotas tq ON tq.tenant_id = t.id
        WHERE t.status != 'deleted'
        ORDER BY t.display_name
        LIMIT 500
        """,
    )

    items: list[_QuotaUsageRow] = []
    for row in rows:
        cap = _safe_int(row["bot_replies_period_cap"])
        used = _safe_int(row["usage_bot_replies_this_period"])
        usage_pct = (used / cap * 100.0) if cap > 0 else 0.0
        items.append(
            _QuotaUsageRow(
                tenant_id=row["tenant_id"],
                tenant_name=row["tenant_name"],
                plan_tier=row["plan_tier"],
                bot_replies=_safe_int(row["bot_replies"]),
                bot_replies_period_cap=cap,
                vision_calls_per_month=_safe_int(row["vision_calls_per_month"]),
                conversation_retention_days=_safe_int(row["conversation_retention_days"]),
                knowledge_chunks_total_mb=_safe_int(row["knowledge_chunks_total_mb"]),
                rate_limit_per_minute=_safe_int(row["rate_limit_per_minute"]),
                monthly_llm_tokens_in=_safe_int(row["monthly_llm_tokens_in"]),
                monthly_llm_tokens_out=_safe_int(row["monthly_llm_tokens_out"]),
                monthly_ingestion_pages=_safe_int(row["monthly_ingestion_pages"]),
                monthly_broadcast_recipients=_safe_int(row["monthly_broadcast_recipients"]),
                monthly_bookings=_safe_int(row["monthly_bookings"]),
                agent_seats_included=_safe_int(row["agent_seats_included"]),
                agent_seats_extra=_safe_int(row["agent_seats_extra"]),
                outbound_broadcast_enabled=bool(row["outbound_broadcast_enabled"]),
                multi_branch_enabled=bool(row["multi_branch_enabled"]),
                allowed_channel_kinds=list(row["allowed_channel_kinds"] or []),
                usage_bot_replies_this_period=used,
                usage_pct=round(usage_pct, 2),
            )
        )

    return _QuotaUsageResponse(items=items, total=len(items))


@router.put("/{tenant_id}/override", response_model=_OverrideResponse)
async def override_quota(
    tenant_id: UUID,
    body: _OverrideRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _OverrideResponse:
    """Override a single quota column for a tenant (ADR-017).

    column must be one of the 16 locked quota column names.
    Any other value returns 422. value=None resets to 0.

    Every successful override emits an audit row with
    action=quota.override carrying before/after values.
    """
    if body.column not in _QUOTA_COLUMNS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"column '{body.column}' is not an overridable quota column. "
                f"Allowed: {sorted(_QUOTA_COLUMNS)}"
            ),
        )

    async with conn.transaction():
        existing = await conn.fetchrow(
            "SELECT id FROM tenants WHERE id = $1 LIMIT 1", tenant_id
        )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found"
            )

        # Read current value for audit before-state.
        # body.column is validated against _QUOTA_COLUMNS (safe identifiers).
        current_row = await conn.fetchrow(
            f"SELECT {body.column} FROM tenant_quotas WHERE tenant_id = $1",  # noqa: S608
            tenant_id,
        )
        before_value: Any = current_row[body.column] if current_row else None

        db_value: Any
        if body.column == "allowed_channel_kinds":
            # The one array column: value must be a list of canonical kinds.
            # asyncpg binds list[str] -> TEXT[] directly; the int/bool/reset-to-0
            # path below must NOT run (0 into TEXT[] is a bind error).
            if not isinstance(body.value, list):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="allowed_channel_kinds requires a list value",
                )
            if not body.value:
                # Mirror the create path's guard — an empty list would lock the
                # tenant out of every channel (a misconfiguration, not an intent).
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="allowed_channel_kinds must not be an empty list",
                )
            unknown = [k for k in body.value if k not in _CANONICAL_CHANNEL_KINDS]
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"unknown channel kind(s) {unknown}; allowed: "
                        f"{sorted(_CANONICAL_CHANNEL_KINDS)}"
                    ),
                )
            # Dedupe, preserving order.
            db_value = list(dict.fromkeys(body.value))
        else:
            # Numeric/boolean columns: reject a list, reset None to 0.
            if isinstance(body.value, list):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"column '{body.column}' does not accept a list value",
                )
            new_value = body.value if body.value is not None else 0
            # Boolean columns: cast int to bool for asyncpg BOOLEAN binding.
            db_value = bool(new_value) if body.column in _BOOLEAN_COLUMNS else new_value

        # UPDATE safe: column validated against _QUOTA_COLUMNS above.
        await conn.execute(
            f"UPDATE tenant_quotas SET {body.column} = $1, updated_at = now() WHERE tenant_id = $2",  # noqa: S608
            db_value,
            tenant_id,
        )

        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="quota.override",
            before={"column": body.column, "value": _coerce_audit_val(before_value)},
            after={"column": body.column, "value": _coerce_audit_val(db_value)},
        )

    overridden_at = datetime.now(UTC).isoformat()
    logger.info(
        "quota.override tenant=%s column=%s new_value=%r actor=%s",
        tenant_id, body.column, db_value, admin_id,
    )

    return _OverrideResponse(
        tenant_id=tenant_id,
        column=body.column,
        new_value=db_value if body.column == "allowed_channel_kinds" else body.value,
        overridden_at=overridden_at,
    )
