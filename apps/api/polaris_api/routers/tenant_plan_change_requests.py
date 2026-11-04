"""Tenant-facing plan-change request API — Phase 8 PR-10.

Mounted at ``/api/tenant/plan-change-requests``. Drives the portal
Billing → plan-change page per 00c §12.4 + ADR-029 (admin approval
required at GA; no self-serve plan flip).

Three endpoints:

  * ``POST /``        — create a pending request. Server reads the
                        tenant's current tier + cycle from `tenants`,
                        computes a proration snapshot, and writes the
                        row. Returns the full ``PlanChangeRequest`` so
                        the portal can immediately render the pending
                        state without a follow-up GET.
  * ``GET  /``        — list the tenant's recent requests (newest first).
                        Drives the Billing page's "pending request"
                        banner.
  * ``GET  /{id}``    — single request detail.

Proration math at GA per 00c §12.4.1: simple linear "days remaining in
current period × monthly tier price". Phase 10 admin recomputes
authoritative on approval; the request-time snapshot is a tenant-
transparency artifact, not the source of truth.

Error envelope codes:
  * ``PLAN_CHANGE_TARGET_SAME_AS_CURRENT`` — target_tier == current_tier
    AND target_billing_cycle == current_billing_cycle. Surfaced as 400
    so the portal can show a clear "you're already on this plan".
  * ``PLAN_CHANGE_NOT_FOUND`` — id RLS-stripped or absent.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from polaris_shared_types import (
    BillingCycle,
    PlanChangeRequest,
    PlanChangeRequestCreate,
    PlanChangeStatus,
    PlanTier,
    ProrationPreview,
)

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-plan-change-requests"])

# Monthly price per tier in MYR. Mirrors ADR-026; if pricing changes
# the migration 051 schema doesn't need updating, only this constant
# (until Phase 10 ships proper pricing-table reads).
_TIER_PRICE_MYR: dict[PlanTier, float] = {
    PlanTier.STARTER:    1099.0,
    PlanTier.PRO:        2499.0,
    PlanTier.ULTRA:      5499.0,
    PlanTier.ENTERPRISE: 0.0,  # custom contract — proration not computed here
}

# Quota top-up (additional bot_replies headroom) granted on tier-up.
# Matches the per-tier monthly cap deltas from the locked 16-column
# tenant_quotas spec.
_TIER_QUOTA_TOPUP: dict[PlanTier, int] = {
    PlanTier.STARTER:    0,
    PlanTier.PRO:        4_000,
    PlanTier.ULTRA:      16_000,
    PlanTier.ENTERPRISE: 0,
}

# Ordering for tier comparison. Higher index = higher tier.
_TIER_ORDER: dict[PlanTier, int] = {
    PlanTier.STARTER: 0,
    PlanTier.PRO: 1,
    PlanTier.ULTRA: 2,
    PlanTier.ENTERPRISE: 3,
}


def _compute_proration(
    *,
    current_tier: PlanTier,
    target_tier: PlanTier,
    days_remaining: int,
    days_in_period: int = 30,
) -> ProrationPreview:
    """Linear proration per 00c §12.4.1.

    Tier-up: charge the new tier's monthly price prorated against days
    remaining in the current period, minus the credit for unused
    current-period days at the old tier price.

    Tier-down: amount_due is 0; the unused current-period credit
    consumes against the next invoice at the new tier (we report 0 due
    here because nothing is charged at the request moment).
    """
    cur_price = _TIER_PRICE_MYR.get(current_tier, 0.0)
    new_price = _TIER_PRICE_MYR.get(target_tier, 0.0)
    fraction = max(0, min(days_remaining, days_in_period)) / max(1, days_in_period)

    is_tier_up = _TIER_ORDER[target_tier] > _TIER_ORDER[current_tier]

    if is_tier_up:
        period_credited = round(cur_price * fraction, 2)
        amount_due = round(max(0.0, new_price * fraction - period_credited), 2)
    else:
        # Tier-down: refund the unused difference but don't charge now.
        period_credited = round(cur_price * fraction, 2)
        amount_due = 0.0

    return ProrationPreview(
        amount_due_myr=amount_due,
        period_credited_myr=period_credited,
        quota_topup_bot_replies=_TIER_QUOTA_TOPUP[target_tier] if is_tier_up else 0,
        prorate_basis="calendar_month",
    )


def _days_remaining_this_month(now: datetime | None = None) -> int:
    """Naïve "days left in the calendar month" — sufficient at GA per
    00c §12.4.1 'calendar_month' basis. Anniversary basis lands in v2.1."""
    now = now or datetime.now(tz=UTC)
    # Last day of month: roll forward to next month then day-=1.
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    last_day = (next_month - now).days + now.day - 1
    return max(0, last_day - now.day + 1)


def _row_to_dto(row: asyncpg.Record) -> PlanChangeRequest:
    proration = row["proration_preview"]
    if isinstance(proration, str):
        proration = json.loads(proration)
    return PlanChangeRequest(
        id=row["id"],
        tenant_id=row["tenant_id"],
        requested_by_user_id=row["requested_by_user_id"],
        current_tier=PlanTier(row["current_tier"]),
        target_tier=PlanTier(row["target_tier"]),
        current_billing_cycle=BillingCycle(row["current_billing_cycle"]),
        target_billing_cycle=BillingCycle(row["target_billing_cycle"]),
        proration_preview=ProrationPreview.model_validate(proration),
        status=PlanChangeStatus(row["status"]),
        reason=row["reason"],
        decided_by_user_id=row["decided_by_user_id"],
        decided_at=row["decided_at"].isoformat() if row["decided_at"] else None,
        decision_note=row["decision_note"],
        created_at=row["created_at"].isoformat(),
    )


@router.post(
    "/",
    response_model=PlanChangeRequest,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a plan-change request (Phase 10 admin approves)",
)
async def create_request(
    body: PlanChangeRequestCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> PlanChangeRequest:
    tenant_row = await conn.fetchrow(
        "SELECT plan_tier FROM tenants WHERE id = $1",
        ctx.tenant_id,
    )
    if tenant_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TENANT_NOT_FOUND"},
        )
    current_tier = PlanTier(tenant_row["plan_tier"])
    # billing_cycle column doesn't exist on `tenants` at GA — every tenant
    # is monthly per ADR-026. The plan_change row records both current +
    # target cycle so a future annual-cycle migration doesn't lose the
    # provenance of the original request.
    current_cycle = BillingCycle.MONTHLY

    if (
        body.target_tier == current_tier
        and body.target_billing_cycle == current_cycle
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PLAN_CHANGE_TARGET_SAME_AS_CURRENT",
                "message": "target_tier + billing_cycle match the current plan",
            },
        )

    days_remaining = _days_remaining_this_month()
    proration = _compute_proration(
        current_tier=current_tier,
        target_tier=body.target_tier,
        days_remaining=days_remaining,
    )

    row = await conn.fetchrow(
        """
        INSERT INTO plan_change_requests
            (tenant_id, requested_by_user_id,
             current_tier, target_tier,
             current_billing_cycle, target_billing_cycle,
             proration_preview, reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
        RETURNING id, tenant_id, requested_by_user_id, current_tier,
                  target_tier, current_billing_cycle, target_billing_cycle,
                  proration_preview, status, reason, decided_by_user_id,
                  decided_at, decision_note, created_at
        """,
        ctx.tenant_id,
        ctx.user_id,
        current_tier.value,
        body.target_tier.value,
        current_cycle.value,
        body.target_billing_cycle.value,
        proration.model_dump(mode="json"),
        body.reason,
    )
    assert row is not None
    logger.info(
        "plan_change.create tenant=%s id=%s target=%s",
        ctx.tenant_id, row["id"], body.target_tier.value,
    )
    return _row_to_dto(row)


@router.get(
    "/",
    response_model=list[PlanChangeRequest],
    summary="List plan-change requests, newest first",
)
async def list_requests(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> list[PlanChangeRequest]:
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, requested_by_user_id, current_tier,
               target_tier, current_billing_cycle, target_billing_cycle,
               proration_preview, status, reason, decided_by_user_id,
               decided_at, decision_note, created_at
        FROM plan_change_requests
        ORDER BY created_at DESC
        """
    )
    return [_row_to_dto(r) for r in rows]


@router.get(
    "/{request_id}",
    response_model=PlanChangeRequest,
    responses={404: {"description": "Request not visible to caller"}},
    summary="Fetch one plan-change request",
)
async def get_request_by_id(
    request_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> PlanChangeRequest:
    row = await conn.fetchrow(
        """
        SELECT id, tenant_id, requested_by_user_id, current_tier,
               target_tier, current_billing_cycle, target_billing_cycle,
               proration_preview, status, reason, decided_by_user_id,
               decided_at, decision_note, created_at
        FROM plan_change_requests
        WHERE id = $1
        """,
        request_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PLAN_CHANGE_NOT_FOUND",
                "message": f"plan-change request {request_id} not found",
            },
        )
    return _row_to_dto(row)
