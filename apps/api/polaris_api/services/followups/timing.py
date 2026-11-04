"""Pure follow-up timing logic — Phase 13 follow-ups PR-1.

No I/O, no clock, no DB — so the load-bearing, fiddly timing rules are
exhaustively unit-testable. Decides WHEN an AI follow-up fires inside
WhatsApp's free 24h service window.

Quiet hours are interpreted in the TENANT's local timezone (via
``zoneinfo``), deliberately UNLIKE ``booking_reminders._apply_quiet_hours``
which compares against UTC — a latent bug for non-UTC tenants (e.g. a MYT
tenant's "22:00" quiet start would be read as 22:00 UTC = 06:00 local).
Tracked separately; this module does it right.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

_ONE_SECOND = timedelta(seconds=1)

# Defaults — Polaris sets quiet hours so tenants don't have to (the
# follow-up config is just an on/off toggle at v1). Tenant-local.
DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(8, 0)
DEFAULT_MIN_DELAY_HOURS = 12
DEFAULT_MAX_DELAY_HOURS = 23  # < 24 leaves a buffer before the hard window close


def is_quiet(t: time, *, quiet_start: time, quiet_end: time) -> bool:
    """Is local time-of-day ``t`` inside the daily quiet window?

    Handles a window that wraps midnight (e.g. 22:00–08:00). ``start ==
    end`` means "no quiet hours" (never quiet).
    """
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= t < quiet_end
    # Wraps midnight: [quiet_start, 24:00) ∪ [00:00, quiet_end).
    return t >= quiet_start or t < quiet_end


def compute_followup_send_time(
    *,
    anchor_inbound_at: datetime,
    tenant_tz: str,
    quiet_start: time = DEFAULT_QUIET_START,
    quiet_end: time = DEFAULT_QUIET_END,
    min_delay_hours: int = DEFAULT_MIN_DELAY_HOURS,
    max_delay_hours: int = DEFAULT_MAX_DELAY_HOURS,
) -> datetime | None:
    """Latest humane send instant in ``[anchor+min, anchor+max]``, or None.

    Returns a tz-aware UTC datetime: the LATEST instant in the window that
    is NOT inside the tenant-local quiet period — i.e. as much time as
    possible for the customer to return on their own, but pulled to just
    before quiet onset / window close so a deadline landing in dead hours
    (e.g. 03:00) fires beforehand (e.g. 21:59).

    Returns ``None`` when the entire window is quiet (no humane slot →
    skip the follow-up rather than send at a bad time).

    ``anchor_inbound_at`` must be timezone-aware (the customer's last
    inbound). ``max_delay_hours`` should stay < 24 so the send lands
    inside the free window; this function honours its params and does not
    clamp to 24h (the worker re-checks the window at send time).
    """
    if anchor_inbound_at.tzinfo is None:
        raise ValueError("anchor_inbound_at must be timezone-aware")
    if min_delay_hours > max_delay_hours:
        return None

    tz = ZoneInfo(tenant_tz)
    window_start = anchor_inbound_at + timedelta(hours=min_delay_hours)
    window_end = anchor_inbound_at + timedelta(hours=max_delay_hours)

    end_local = window_end.astimezone(tz)
    if not is_quiet(end_local.time(), quiet_start=quiet_start, quiet_end=quiet_end):
        # Deadline is in active hours → use it (max give-time).
        return window_end

    # Deadline is in quiet hours → pull back to just before this quiet
    # period's onset (the latest active instant <= window_end).
    onset_local = end_local.replace(
        hour=quiet_start.hour,
        minute=quiet_start.minute,
        second=0,
        microsecond=0,
        fold=1,  # pick the later occurrence on a DST "fall back" night
    )
    if onset_local > end_local:
        onset_local -= timedelta(days=1)
    active_utc = (onset_local - _ONE_SECOND).astimezone(UTC)

    if active_utc >= window_start:
        return active_utc
    # The whole window falls inside the quiet period → no humane slot.
    return None
