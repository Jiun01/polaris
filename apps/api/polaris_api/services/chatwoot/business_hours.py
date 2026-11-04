"""Shared ADR-019 business-hours window logic.

``chatwoot/handoff.is_in_business_hours`` resolves the schedule shape (one
entry per day-of-week, 0=Monday … 6=Sunday) against a tenant-local ``now``.
This module holds the single parse + window-membership implementation.

Schedule shape::

    {
      "0": {"open": "09:00", "close": "18:00"},
      ...
      "5": null,
      "6": null
    }

A missing or NULL day entry counts as "closed". Invalid time strings also fall
through to "closed".
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping


def is_within_schedule(
    *,
    business_hours: Mapping[str, object],
    now: datetime.datetime,
    grace_minutes: int = 0,
) -> bool:
    """``True`` iff ``now`` (tenant-local, tz-aware) falls inside the day's
    open/close window.

    ``grace_minutes`` widens the window symmetrically (both open and close).
    A missing/NULL day entry or an unparseable time string returns ``False``.
    """
    day_key = str(now.weekday())
    entry = business_hours.get(day_key)
    # Falsy (None / empty dict) or non-dict day entries → closed.
    if not entry or not isinstance(entry, dict):
        return False
    try:
        open_h, open_m = (int(p) for p in str(entry["open"]).split(":"))
        close_h, close_m = (int(p) for p in str(entry["close"]).split(":"))
        open_t = datetime.time(open_h, open_m)
        close_t = datetime.time(close_h, close_m)
    except (KeyError, ValueError, TypeError):
        return False
    if grace_minutes <= 0:
        return open_t <= now.time().replace(microsecond=0) < close_t
    # Apply grace by widening with timedelta. Need full datetimes to do
    # arithmetic that crosses midnight cleanly.
    today = now.date()
    tz = now.tzinfo
    open_dt = datetime.datetime.combine(today, open_t, tz)
    close_dt = datetime.datetime.combine(today, close_t, tz)
    grace = datetime.timedelta(minutes=grace_minutes)
    return open_dt - grace <= now < close_dt + grace
