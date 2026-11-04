from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from polaris_api.services.billing.proration import (
    BillingCycle,
    PlanTier,
    ProrationInputs,
    compute,
)

# ---------------------------------------------------------------------------
# Annual path — no proration
# ---------------------------------------------------------------------------

def test_annual_pro_full_price() -> None:
    result = compute(ProrationInputs(
        target_tier=PlanTier.PRO,
        billing_cycle=BillingCycle.ANNUAL,
    ))
    # 1176 * 12 * 0.85 = 11995.20
    assert result.amount_due_myr == Decimal("11995.20")
    assert result.ratio == Decimal("1")
    assert result.days_in_period == 365
    assert result.days_billed == 365
    assert result.headline_reply_cap == 5_000


def test_annual_starter_full_price() -> None:
    result = compute(ProrationInputs(
        target_tier=PlanTier.STARTER,
        billing_cycle=BillingCycle.ANNUAL,
    ))
    # 822 * 12 * 0.85 = 8384.40
    assert result.amount_due_myr == Decimal("8384.40")


def test_annual_ultra_full_price() -> None:
    result = compute(ProrationInputs(
        target_tier=PlanTier.ULTRA,
        billing_cycle=BillingCycle.ANNUAL,
    ))
    # 3058 * 12 * 0.85 = 31191.60
    assert result.amount_due_myr == Decimal("31191.60")


# ---------------------------------------------------------------------------
# Monthly signup (new tenant, no current_tier)
# ---------------------------------------------------------------------------

def test_monthly_signup_first_day_of_month() -> None:
    # First day: full month charged
    result = compute(ProrationInputs(
        target_tier=PlanTier.PRO,
        billing_cycle=BillingCycle.MONTHLY,
        current_tier=None,
        period_start=date(2026, 5, 1),
        today=date(2026, 5, 1),
    ))
    # 31 days in May, today=1 => days_left = 31, ratio = 1.0
    assert result.ratio == Decimal("1")
    assert result.amount_due_myr == Decimal("1176.00")


def test_monthly_signup_mid_month() -> None:
    # May has 31 days; today=16 => days_left=31-16+1=16, ratio=16/31
    result = compute(ProrationInputs(
        target_tier=PlanTier.PRO,
        billing_cycle=BillingCycle.MONTHLY,
        current_tier=None,
        period_start=date(2026, 5, 1),
        today=date(2026, 5, 16),
    ))
    expected_ratio = Decimal(16) / Decimal(31)
    assert result.ratio == expected_ratio
    assert result.days_billed == 16
    assert result.days_in_period == 31


def test_monthly_signup_last_day() -> None:
    # Last day of month => 1 day left, minimum charge
    result = compute(ProrationInputs(
        target_tier=PlanTier.STARTER,
        billing_cycle=BillingCycle.MONTHLY,
        current_tier=None,
        period_start=date(2026, 5, 1),
        today=date(2026, 5, 31),
    ))
    assert result.days_billed == 1
    assert result.days_in_period == 31


# ---------------------------------------------------------------------------
# Monthly upgrade
# ---------------------------------------------------------------------------

def test_monthly_upgrade_starter_to_pro() -> None:
    # Upgrade Starter->Pro on day 11 of May (31 days)
    # days_left = 31 - 11 + 1 = 21, ratio = 21/31
    # delta_price = 1176 - 822 = 354; amount = round(354 * 21/31, 2)
    result = compute(ProrationInputs(
        target_tier=PlanTier.PRO,
        billing_cycle=BillingCycle.MONTHLY,
        current_tier=PlanTier.STARTER,
        period_start=date(2026, 5, 1),
        today=date(2026, 5, 11),
    ))
    ratio = Decimal(21) / Decimal(31)
    expected = (Decimal("354") * ratio).quantize(Decimal("0.01"))
    # Use rounding ROUND_HALF_UP (same as proration module)
    from decimal import ROUND_HALF_UP
    expected = (Decimal("354") * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert result.amount_due_myr == expected
    assert result.quota_topup_bot_replies > 0


# ---------------------------------------------------------------------------
# Monthly downgrade -> amount_due = 0
# ---------------------------------------------------------------------------

def test_monthly_downgrade_returns_zero_due() -> None:
    result = compute(ProrationInputs(
        target_tier=PlanTier.STARTER,
        billing_cycle=BillingCycle.MONTHLY,
        current_tier=PlanTier.PRO,
        period_start=date(2026, 5, 1),
        today=date(2026, 5, 15),
    ))
    assert result.amount_due_myr == Decimal("0.00")
    assert result.quota_topup_bot_replies == 0


def test_monthly_downgrade_ultra_to_starter() -> None:
    result = compute(ProrationInputs(
        target_tier=PlanTier.STARTER,
        billing_cycle=BillingCycle.MONTHLY,
        current_tier=PlanTier.ULTRA,
        period_start=date(2026, 5, 1),
        today=date(2026, 5, 20),
    ))
    assert result.amount_due_myr == Decimal("0.00")


# ---------------------------------------------------------------------------
# Missing period_start/today raises
# ---------------------------------------------------------------------------

def test_monthly_missing_period_raises() -> None:
    with pytest.raises(ValueError, match="period_start"):
        compute(ProrationInputs(
            target_tier=PlanTier.PRO,
            billing_cycle=BillingCycle.MONTHLY,
        ))


# ---------------------------------------------------------------------------
# February (28-day month)
# ---------------------------------------------------------------------------

def test_february_non_leap() -> None:
    result = compute(ProrationInputs(
        target_tier=PlanTier.PRO,
        billing_cycle=BillingCycle.MONTHLY,
        current_tier=None,
        period_start=date(2026, 2, 1),
        today=date(2026, 2, 15),
    ))
    # Feb 2026 = 28 days; today=15 => days_left=28-15+1=14
    assert result.days_in_period == 28
    assert result.days_billed == 14
