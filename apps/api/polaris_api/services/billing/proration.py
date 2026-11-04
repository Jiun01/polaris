"""Pure proration math for Polaris billing — Phase 9 PR-7/8/9.

No DB access. Called by admin_billing.py (new invoices), admin_plan_changes.py
(server-authoritative recompute on approval), and admin_quotes.py (sales quotes).

Proration spec: PRD §12.4.1.

Tier prices (MYR/month):
  Starter=822, Pro=1176, Ultra=3058, Enterprise=0 (custom)
  (Monthly headline; annual = ×12×0.85 ≈ RM 699 / 999 / 2,599 effective/mo.)

Tier monthly bot_replies quotas:
  Starter=1500, Pro=5000, Ultra=15000, Enterprise=0 (custom)

Annual discount: multiply monthly price by 0.85 per year (pay upfront).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum


class PlanTier(StrEnum):
    STARTER = "starter"
    PRO = "pro"
    ULTRA = "ultra"
    ENTERPRISE = "enterprise"


class BillingCycle(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


# Monthly headline price in MYR (Decimal for precision).
_TIER_PRICE: dict[PlanTier, Decimal] = {
    PlanTier.STARTER:    Decimal("822"),
    PlanTier.PRO:        Decimal("1176"),
    PlanTier.ULTRA:      Decimal("3058"),
    PlanTier.ENTERPRISE: Decimal("0"),  # custom contract
}

# Full-month bot_replies quota per tier.
_TIER_QUOTA: dict[PlanTier, int] = {
    PlanTier.STARTER:    1_500,
    PlanTier.PRO:        5_000,
    PlanTier.ULTRA:      15_000,
    PlanTier.ENTERPRISE: 0,  # custom contract
}

_ANNUAL_DISCOUNT = Decimal("0.85")  # 15% off when paying annually upfront

# Tier ordering for upgrade/downgrade detection.
_TIER_ORDER: dict[PlanTier, int] = {
    PlanTier.STARTER:    0,
    PlanTier.PRO:        1,
    PlanTier.ULTRA:      2,
    PlanTier.ENTERPRISE: 3,
}

_TWO_DP = Decimal("0.01")


def _round2(v: Decimal) -> Decimal:
    return v.quantize(_TWO_DP, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ProrationInputs:
    """Input bag for compute().

    current_tier=None means new-tenant signup (no existing plan).
    signup_day overrides "today" for signup proration; when None compute()
    uses today's day from period_start if that is set, otherwise raises.

    For mid-month upgrade/downgrade:
      - period_start: first day of the billing period being adjusted
      - today: the actual calendar date of the change (determines days_left)
    """

    target_tier: PlanTier
    billing_cycle: BillingCycle

    # For upgrades/downgrades: the current plan.
    current_tier: PlanTier | None = None

    # Which day of which month the event occurs (used to derive ratio).
    # Both must be set together for mid-month events.
    period_start: date | None = None
    today: date | None = None


@dataclass(frozen=True)
class ProrationResult:
    """Result of compute()."""

    amount_due_myr: Decimal
    quota_topup_bot_replies: int
    days_in_period: int
    days_billed: int
    ratio: Decimal
    headline_price_myr: Decimal
    headline_reply_cap: int


def compute(inputs: ProrationInputs) -> ProrationResult:
    """Server-authoritative proration computation (pure, no DB).

    Handles three cases:
      1. New signup (current_tier=None): pro-rated first invoice.
      2. Upgrade (target_tier > current_tier): pro-rated delta invoice.
      3. Downgrade (target_tier < current_tier): amount_due=0.
      4. Annual: no proration — full year price, full month quota.
    """
    target_price = _TIER_PRICE[inputs.target_tier]
    target_quota = _TIER_QUOTA[inputs.target_tier]

    headline_price: Decimal
    if inputs.billing_cycle == BillingCycle.ANNUAL:
        # Annual: 12 × monthly × 0.85, upfront, no proration.
        headline_price = _round2(target_price * 12 * _ANNUAL_DISCOUNT)
        return ProrationResult(
            amount_due_myr=headline_price,
            quota_topup_bot_replies=target_quota,
            days_in_period=365,
            days_billed=365,
            ratio=Decimal("1"),
            headline_price_myr=target_price,
            headline_reply_cap=target_quota,
        )

    # Monthly path — need period_start and today.
    if inputs.period_start is None or inputs.today is None:
        raise ValueError(
            "period_start and today are required for monthly proration"
        )
    period_start = inputs.period_start
    today = inputs.today

    days_in_month = calendar.monthrange(period_start.year, period_start.month)[1]
    # days_left = days remaining including today.
    days_left = days_in_month - today.day + 1
    days_left = max(1, min(days_left, days_in_month))

    ratio = Decimal(days_left) / Decimal(days_in_month)

    if inputs.current_tier is None:
        # Case 1: new signup.
        amount_due = _round2(target_price * ratio)
        quota_topup = int(target_quota * ratio)
        return ProrationResult(
            amount_due_myr=amount_due,
            quota_topup_bot_replies=quota_topup,
            days_in_period=days_in_month,
            days_billed=days_left,
            ratio=ratio,
            headline_price_myr=target_price,
            headline_reply_cap=target_quota,
        )

    current_price = _TIER_PRICE[inputs.current_tier]
    current_quota = _TIER_QUOTA[inputs.current_tier]
    is_upgrade = _TIER_ORDER[inputs.target_tier] > _TIER_ORDER[inputs.current_tier]

    if not is_upgrade:
        # Case 3: downgrade — no mid-cycle charge.
        return ProrationResult(
            amount_due_myr=Decimal("0.00"),
            quota_topup_bot_replies=0,
            days_in_period=days_in_month,
            days_billed=days_left,
            ratio=ratio,
            headline_price_myr=target_price,
            headline_reply_cap=target_quota,
        )

    # Case 2: upgrade — charge the delta for days remaining.
    delta_price = target_price - current_price
    amount_due = _round2(delta_price * ratio)
    quota_topup = int((target_quota - current_quota) * ratio)

    return ProrationResult(
        amount_due_myr=amount_due,
        quota_topup_bot_replies=quota_topup,
        days_in_period=days_in_month,
        days_billed=days_left,
        ratio=ratio,
        headline_price_myr=target_price,
        headline_reply_cap=target_quota,
    )
