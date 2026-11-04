"""Pure timing tests — Phase 13 follow-ups PR-1.

Spec-as-tests for ``compute_followup_send_time``. All cases use
Asia/Kuala_Lumpur (UTC+8, no DST) for deterministic local arithmetic, with
the default quiet window 22:00–08:00 unless a case overrides it.

The headline behaviour (your ask): when the 23h deadline lands in dead
hours, the follow-up fires BEFORE quiet onset rather than at 3am or being
skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from polaris_api.services.followups.timing import (
    compute_followup_send_time,
    is_quiet,
)

_TZ = "Asia/Kuala_Lumpur"  # UTC+8, no DST
_UTC = UTC


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=_UTC)


# ---------------------------------------------------------------------
# is_quiet
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "t,expected",
    [
        (time(23, 0), True),    # inside wrap (evening)
        (time(3, 0), True),     # inside wrap (early morning)
        (time(22, 0), True),    # boundary start (inclusive)
        (time(8, 0), False),    # boundary end (exclusive)
        (time(12, 0), False),   # midday active
        (time(21, 59), False),  # just before onset
    ],
)
def test_is_quiet_wrapping_window(t: time, expected: bool) -> None:
    assert is_quiet(t, quiet_start=time(22, 0), quiet_end=time(8, 0)) is expected


def test_is_quiet_non_wrapping_window() -> None:
    assert is_quiet(time(3, 0), quiet_start=time(1, 0), quiet_end=time(6, 0)) is True
    assert is_quiet(time(7, 0), quiet_start=time(1, 0), quiet_end=time(6, 0)) is False


def test_is_quiet_equal_bounds_is_never_quiet() -> None:
    assert is_quiet(time(3, 0), quiet_start=time(0, 0), quiet_end=time(0, 0)) is False


# ---------------------------------------------------------------------
# compute_followup_send_time — headline cases
# ---------------------------------------------------------------------


def test_deadline_in_dead_hours_pulls_back_before_quiet() -> None:
    # Anchor 2026-06-02 06:00 MYT (= 06-01 22:00 UTC).
    # window = [06-02 18:00 MYT, 06-03 05:00 MYT]; deadline 05:00 is dead.
    # → fire at 06-02 21:59:59 MYT (before 10pm) = 06-02 13:59:59 UTC.
    anchor = _utc(2026, 6, 1, 22, 0)
    result = compute_followup_send_time(anchor_inbound_at=anchor, tenant_tz=_TZ)
    assert result == _utc(2026, 6, 2, 13, 59, 59)


def test_deadline_in_active_hours_uses_max_give_time() -> None:
    # Anchor 06-01 10:00 MYT (= 06-01 02:00 UTC); deadline +23h = 06-02
    # 09:00 MYT (active) → send exactly at the 23h cap.
    anchor = _utc(2026, 6, 1, 2, 0)
    result = compute_followup_send_time(anchor_inbound_at=anchor, tenant_tz=_TZ)
    assert result == _utc(2026, 6, 2, 1, 0)  # 06-02 09:00 MYT


def test_whole_window_quiet_returns_none() -> None:
    # Wide quiet 20:00–09:00 (13h) > 11h window → window can be fully dead.
    # Anchor 06-01 09:00 MYT (= 06-01 01:00 UTC):
    # window = [06-01 21:00 MYT, 06-02 08:00 MYT] — all inside 20:00–09:00.
    anchor = _utc(2026, 6, 1, 1, 0)
    result = compute_followup_send_time(
        anchor_inbound_at=anchor,
        tenant_tz=_TZ,
        quiet_start=time(20, 0),
        quiet_end=time(9, 0),
    )
    assert result is None


def test_non_wrapping_quiet_pulls_back() -> None:
    # Quiet 01:00–06:00. Anchor 06-02 04:00 MYT (= 06-01 20:00 UTC):
    # deadline +23h = 06-03 03:00 MYT (inside 01:00–06:00) →
    # fire 06-03 00:59:59 MYT = 06-02 16:59:59 UTC (>= window_start 06-02 16:00 MYT).
    anchor = _utc(2026, 6, 1, 20, 0)
    result = compute_followup_send_time(
        anchor_inbound_at=anchor,
        tenant_tz=_TZ,
        quiet_start=time(1, 0),
        quiet_end=time(6, 0),
    )
    assert result == _utc(2026, 6, 2, 16, 59, 59)  # 06-03 00:59:59 MYT


def test_result_is_within_window_and_before_24h_close() -> None:
    anchor = _utc(2026, 6, 1, 22, 0)
    result = compute_followup_send_time(anchor_inbound_at=anchor, tenant_tz=_TZ)
    assert result is not None
    assert anchor + timedelta(hours=12) <= result
    assert result < anchor + timedelta(hours=24)


# ---------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------


def test_naive_anchor_raises() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_followup_send_time(
            anchor_inbound_at=datetime(2026, 6, 1, 22, 0), tenant_tz=_TZ
        )


def test_min_greater_than_max_returns_none() -> None:
    anchor = _utc(2026, 6, 1, 22, 0)
    result = compute_followup_send_time(
        anchor_inbound_at=anchor, tenant_tz=_TZ, min_delay_hours=23, max_delay_hours=12
    )
    assert result is None


def test_no_quiet_hours_uses_max_give_time() -> None:
    # start == end → never quiet → always the 23h cap.
    anchor = _utc(2026, 6, 1, 22, 0)
    result = compute_followup_send_time(
        anchor_inbound_at=anchor,
        tenant_tz=_TZ,
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    assert result == anchor + timedelta(hours=23)
