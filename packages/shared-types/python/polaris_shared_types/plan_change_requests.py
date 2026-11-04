"""Portal-submitted plan-change request shapes — Pydantic SSOT (Phase 8 PR-1).

Tenant submits a plan-change request via ``POST /api/tenant/plan-change-requests``;
reads status via ``GET /api/tenant/plan-change-requests/{id}``. Approval
runs in Phase 10's admin path — at GA this is captured as schema only,
with admin-side approval handlers stubbed (operator manually approves via
direct DB write + sends a ``plan_change_approved`` notification row per
phase-checklist/phase-08-portal.md risk register).

Anchors:
  - ADR-026 (tier pricing — RM 1,099 / 2,499 / 5,499)
  - ADR-029 (no self-serve plan change at GA — admin approval required)
  - 00c §12.4 / §12.4.1 (plan-change request flow + proration mechanics)

Backing table: ``plan_change_requests`` (migration 051).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class PlanTier(StrEnum):
    STARTER    = "starter"
    PRO        = "pro"
    ULTRA      = "ultra"
    ENTERPRISE = "enterprise"


class BillingCycle(StrEnum):
    MONTHLY = "monthly"
    ANNUAL  = "annual"


class PlanChangeStatus(StrEnum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"
    CANCELLED = "cancelled"


class ProrationPreview(BaseModel):
    """Snapshot computed at request time per 00c §12.4.1.

    Phase 10 admin recomputes authoritative on approval; the request-time
    snapshot is a tenant-transparency artifact, not the source of truth.
    If admin's recomputed amount diverges from this snapshot (e.g. tier
    pricing changed between request + approval), admin path surfaces the
    delta in ``decision_note`` so the tenant sees why.
    """

    model_config = _FORBID

    amount_due_myr: float = Field(
        ...,
        ge=0,
        description=(
            "Net charge for the change. 0 = tier downgrade with prorated "
            "credit consumed by next invoice."
        ),
    )
    period_credited_myr: float = Field(
        ...,
        ge=0,
        description=(
            "Unused current-period amount credited towards the new tier. "
            "0 if request fires at period boundary."
        ),
    )
    quota_topup_bot_replies: int = Field(
        ...,
        ge=0,
        description=(
            "Additional bot_replies headroom granted immediately on "
            "approval (tier-up only; tier-down is 0)."
        ),
    )
    prorate_basis: Literal["calendar_month", "anniversary"] = Field(
        "calendar_month",
        description=(
            "How the proration was computed. GA uses calendar_month "
            "(invoice fires on the 1st per 00c §12.4.1). "
            "'anniversary' is the v2.1+ option for tenants on a custom "
            "billing date."
        ),
    )


class PlanChangeRequestCreate(BaseModel):
    """Request body for ``POST /api/tenant/plan-change-requests``.

    Tenant supplies only target tier + cycle + optional reason. Server
    fills current_tier/current_billing_cycle from tenant_quotas + computes
    proration_preview at insert time.
    """

    model_config = _FORBID

    target_tier: PlanTier
    target_billing_cycle: BillingCycle
    reason: str | None = Field(
        None,
        max_length=1000,
        description="Optional tenant message; surfaces in admin approval UI.",
    )


class PlanChangeRequest(BaseModel):
    """Read shape for ``GET /api/tenant/plan-change-requests/{id}`` and
    response body of ``POST /api/tenant/plan-change-requests``.

    All timestamps RFC3339 UTC strings (asyncpg datetimes are isoformatted
    at the router layer).
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    requested_by_user_id: UUID
    current_tier: PlanTier
    target_tier: PlanTier
    current_billing_cycle: BillingCycle
    target_billing_cycle: BillingCycle
    proration_preview: ProrationPreview
    status: PlanChangeStatus
    reason: str | None
    decided_by_user_id: UUID | None = Field(
        None,
        description="Phase 10 admin auth.users.id; NULL while pending.",
    )
    decided_at: str | None = Field(
        None,
        description="RFC3339 UTC; NULL while pending.",
    )
    decision_note: str | None
    created_at: str


__all__ = [
    "BillingCycle",
    "PlanChangeRequest",
    "PlanChangeRequestCreate",
    "PlanChangeStatus",
    "PlanTier",
    "ProrationPreview",
]
