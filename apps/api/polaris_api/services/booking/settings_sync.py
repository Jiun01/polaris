"""EA → Polaris booking-settings mirror — Phase 8 follow-on.

Easy!Appointments (the Calendar admin app) is the **source of truth** for a
tenant's booking policy + schedule. This service reads a tenant's EA settings +
blocked periods and mirrors them into ``tenants.settings`` so the bot's existing
reader (:func:`read_booking_config`) enforces EA's values — without ever putting
EA on the bot's hot path:

  * **Policy** keys EA owns (cancellation/reschedule lead-time, timezone) are
    overlaid into ``booking_config`` — the dict the bot already reads. The two
    lead-time fields **collapse** onto EA's single ``book_advance_timeout``.
  * A richer **display** snapshot (working plan + breaks, blocked periods,
    future-booking limit, timezone, sync timestamps) is written to a new
    ``ea_calendar`` block for the portal's read-only panel + "next refresh"
    countdown.

Bot-only ``booking_config`` keys with no EA equivalent (confirmation message,
self-serve toggle, reminders, quiet hours, WhatsApp templates) are **preserved**
— the EA-derived keys are overlaid onto the current blob, not a fresh one.

Like ``account_provisioning``, this NEVER raises into the caller: EA being
unreachable must not break a scheduled tick, a provisioning flow, or a portal
request. On any EA error the stale mirror simply stands until the next
successful sync.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from polaris_api.services.booking.client import (
    EABookingClient,
    EABookingError,
    get_ea_booking_client,
)

logger = logging.getLogger(__name__)

# Default cadence for the scheduled refresh; the portal countdown is driven by
# the ``next_refresh_at`` we stamp, so a caller that ticks on a different cadence
# must pass its real interval here for the countdown to line up.
DEFAULT_REFRESH_INTERVAL_S = 3600

# BookingConfig caps lead-time at 168h (le=168); clamp the EA-derived value so a
# pathological EA setting can't produce an out-of-range mirror the portal then
# fails to validate.
_MAX_LEAD_HOURS = 168

# booking_config keys this service owns (EA source of truth). Everything else in
# booking_config is bot-only and must survive a sync untouched.
EA_OWNED_BOOKING_CONFIG_KEYS = (
    "cancellation_lead_hours",
    "reschedule_window_hours",
    "tenant_timezone",
)


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a single EA → Polaris mirror attempt.

    ``ok`` is True only when EA was read and the mirror was written. On a no-op
    (tenant has no active EA account) or any EA error, ``ok`` is False and
    ``reason`` explains why; the existing mirror is left untouched.
    """

    ok: bool
    reason: str | None = None
    synced_at: str | None = None
    next_refresh_at: str | None = None


def _settings_to_dict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten EA's ``[{name,value}]`` collection into ``{name: value}``."""
    out: dict[str, Any] = {}
    for row in rows:
        name = row.get("name")
        if isinstance(name, str):
            out[name] = row.get("value")
    return out


def _parse_working_plan(raw: Any) -> dict[str, Any] | None:
    """Parse EA's ``company_working_plan`` JSON string into a dict.

    EA stores the plan as a JSON string keyed by weekday, each day either an
    object ``{start,end,breaks:[...]}`` or ``null`` when closed. Returns None on
    a missing/blank/malformed value so the portal renders "not configured"
    rather than crashing (the bug that motivated the provisioning-seed plan).
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("settings_sync: malformed company_working_plan; skipping")
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_lead_hours(raw: Any) -> int | None:
    """Convert EA ``book_advance_timeout`` (minutes, string) to clamped hours.

    Rounds UP to whole hours: BookingConfig is integer hours but EA stores
    minutes, so any positive sub-hour timeout (EA's default is 30 min) must map
    to ≥1h, never 0. ``round()`` would collapse 30 min → 0h (Python banker's
    rounding: ``round(0.5) == 0``), silently dropping the cancel window. Ceiling
    also keeps the bot at-least-as-strict as the calendar, so the bot never
    offers a cancel/reschedule that EA then rejects.
    """
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if minutes < 0:
        return None
    if minutes == 0:
        return 0
    hours = math.ceil(minutes / 60)
    return min(hours, _MAX_LEAD_HOURS)


def _coerce_int(raw: Any) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _map_blocked_periods(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project EA blocked-period rows to the display shape ``{start,end,notes,name}``."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "start": row.get("start"),
                "end": row.get("end"),
                "notes": row.get("notes") or "",
                "name": row.get("name") or "",
            }
        )
    return out


def build_mirror(
    settings_rows: list[dict[str, Any]],
    blocked_rows: list[dict[str, Any]],
    *,
    now: datetime,
    interval_s: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build ``(ea_calendar, ea_policy)`` from raw EA reads.

    ``ea_calendar`` is the full display snapshot; ``ea_policy`` is the subset of
    EA-owned ``booking_config`` keys to overlay (only keys we could derive are
    present, so a missing EA value never clobbers an existing one with junk).
    Pure + deterministic for ``now`` so the mapping is unit-testable without a
    clock or a DB.
    """
    settings = _settings_to_dict(settings_rows)

    lead_hours = _coerce_lead_hours(settings.get("book_advance_timeout"))
    timezone = settings.get("default_timezone") or None
    working_plan = _parse_working_plan(settings.get("company_working_plan"))
    future_limit_days = _coerce_int(settings.get("future_booking_limit"))
    blocked = _map_blocked_periods(blocked_rows)

    next_refresh = now + timedelta(seconds=interval_s)
    ea_calendar: dict[str, Any] = {
        "lead_time_hours": lead_hours,
        "timezone": timezone,
        "working_plan": working_plan,
        "blocked_periods": blocked,
        "future_booking_limit_days": future_limit_days,
        "synced_at": now.isoformat(),
        "next_refresh_at": next_refresh.isoformat(),
        "refresh_interval_s": interval_s,
    }

    # Only overlay keys we actually derived — collapse cancel + reschedule onto
    # EA's single book_advance_timeout.
    ea_policy: dict[str, Any] = {}
    if lead_hours is not None:
        ea_policy["cancellation_lead_hours"] = lead_hours
        ea_policy["reschedule_window_hours"] = lead_hours
    if timezone is not None:
        ea_policy["tenant_timezone"] = timezone

    return ea_calendar, ea_policy


@dataclass(frozen=True)
class _EaReadState:
    """Result of ``_read_ea_state`` — either an early no-op reason, or the
    tenant's current ``settings`` dict to carry into the HTTP + write phases."""

    settings_raw: dict[str, Any] | None
    early_result: SyncResult | None = None


async def _read_ea_state(conn: asyncpg.Connection, tenant_id: UUID) -> _EaReadState:
    """Phase 1 (DB, no HTTP): read ``tenants.settings`` + the ``ea_account`` gate.

    Returns ``early_result`` set (and ``settings_raw=None``) when the caller
    should skip the EA HTTP phase entirely (tenant missing / no active EA
    account) — mirrors the early-return branches ``sync_ea_calendar`` used to
    inline before the phase split (audit Batch B / B7-a).
    """
    row = await conn.fetchrow(
        "SELECT settings FROM tenants WHERE id = $1 LIMIT 1", tenant_id
    )
    if row is None:
        return _EaReadState(
            settings_raw=None,
            early_result=SyncResult(ok=False, reason="tenant_not_found"),
        )

    settings_raw = row["settings"]
    if isinstance(settings_raw, str):
        try:
            settings_raw = json.loads(settings_raw)
        except json.JSONDecodeError:
            settings_raw = {}
    if not isinstance(settings_raw, dict):
        settings_raw = {}

    ea_account = settings_raw.get("ea_account")
    if not isinstance(ea_account, dict) or ea_account.get("status") != "active":
        return _EaReadState(
            settings_raw=None,
            early_result=SyncResult(ok=False, reason="no_active_ea_account"),
        )

    return _EaReadState(settings_raw=settings_raw)


async def _write_mirror(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    ea_calendar: dict[str, Any],
    ea_policy: dict[str, Any],
) -> None:
    """Phase 3 (DB, no HTTP): overlay EA-owned keys + write the display snapshot.

    Re-reads ``settings->'booking_config'`` under THIS conn (not the phase-1
    read) so a portal PUT that landed during the EA HTTP window isn't
    clobbered beyond today's documented read-modify-write tolerance — the
    pooled variant's phase-1 and phase-3 reads may now be seconds apart
    (audit Batch B / B7-a), where before they were the same connection's
    single read.
    """
    current_row = await conn.fetchrow(
        "SELECT settings->'booking_config' AS booking_config "
        "FROM tenants WHERE id = $1 LIMIT 1",
        tenant_id,
    )
    current_bc_raw = current_row["booking_config"] if current_row is not None else None
    if isinstance(current_bc_raw, str):
        try:
            current_bc_raw = json.loads(current_bc_raw)
        except json.JSONDecodeError:
            current_bc_raw = None
    merged_bc: dict[str, Any] = (
        dict(current_bc_raw) if isinstance(current_bc_raw, dict) else {}
    )
    merged_bc.update(ea_policy)

    # Bind dicts (asyncpg JSONB codec serialises once — json.dumps here would
    # double-encode, breaking ``settings->>'...'`` reads).
    await conn.execute(
        """
        UPDATE tenants
           SET settings =
                 (CASE WHEN jsonb_typeof(settings) = 'object'
                       THEN settings ELSE '{}'::jsonb END)
                 || jsonb_build_object('ea_calendar', $2::jsonb)
                 || jsonb_build_object('booking_config', $3::jsonb),
               updated_at = now()
         WHERE id = $1
        """,
        tenant_id,
        ea_calendar,
        merged_bc,
    )


async def sync_ea_calendar(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    ea_client: EABookingClient | None = None,
    interval_s: int = DEFAULT_REFRESH_INTERVAL_S,
) -> SyncResult:
    """Mirror a tenant's EA booking settings into ``tenants.settings``; never raise.

    No-ops cleanly when the tenant has no active EA account. On EA error returns
    ``ok=False`` and leaves the existing mirror untouched. On success writes the
    ``ea_calendar`` snapshot and overlays the EA-owned policy keys onto the
    existing ``booking_config`` (bot-only keys preserved).

    Composes the three phases (``_read_ea_state`` → EA HTTP → ``_write_mirror``)
    on ONE connection, same as before the B7-a split — this is the signature
    ``routers/tenant_bookings.py`` and ``booking/account_provisioning.py``
    keep using. ``sync_ea_calendar_pooled`` is the scheduled-job variant that
    releases the conn between phases 1 and 3.
    """
    client = ea_client or get_ea_booking_client()

    state = await _read_ea_state(conn, tenant_id)
    if state.early_result is not None:
        return state.early_result

    try:
        settings_rows = await client.get_settings(tenant_id=tenant_id)
        blocked_rows = await client.list_blocked_periods(tenant_id=tenant_id)
    except EABookingError as exc:
        reason = type(exc).__name__
        logger.warning(
            "sync_ea_calendar: EA read failed tenant=%s reason=%s msg=%s",
            tenant_id,
            reason,
            exc,
        )
        return SyncResult(ok=False, reason=reason)

    now = datetime.now(UTC)
    ea_calendar, ea_policy = build_mirror(
        settings_rows, blocked_rows, now=now, interval_s=interval_s
    )

    await _write_mirror(
        conn, tenant_id=tenant_id, ea_calendar=ea_calendar, ea_policy=ea_policy,
    )

    logger.info(
        "sync_ea_calendar: tenant=%s lead=%sh tz=%s blocked=%d next_refresh=%s",
        tenant_id,
        ea_calendar.get("lead_time_hours"),
        ea_calendar.get("timezone"),
        len(ea_calendar.get("blocked_periods") or []),
        ea_calendar.get("next_refresh_at"),
    )
    return SyncResult(
        ok=True,
        synced_at=ea_calendar["synced_at"],
        next_refresh_at=ea_calendar["next_refresh_at"],
    )


async def sync_ea_calendar_pooled(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    interval_s: int = DEFAULT_REFRESH_INTERVAL_S,
    ea_client: EABookingClient | None = None,
) -> SyncResult:
    """Scheduled-job variant of ``sync_ea_calendar`` (audit Batch B / B7-a).

    Acquires a service-pool conn for phase 1 (read), RELEASES it, makes the
    two (slow) EA HTTP calls with NO conn held, then re-acquires a
    (possibly different) conn for phase 3 (write). This is what
    ``scheduled/ea_settings_sync.py``'s per-tenant loop calls — the whole
    point of the split is that a scheduled sweep over N tenants no longer
    holds one of the 4 service-pool slots for the duration of N sequential
    EA round-trips.
    """
    client = ea_client or get_ea_booking_client()

    async with pool.acquire() as conn:
        state = await _read_ea_state(conn, tenant_id)
    if state.early_result is not None:
        return state.early_result

    try:
        settings_rows = await client.get_settings(tenant_id=tenant_id)
        blocked_rows = await client.list_blocked_periods(tenant_id=tenant_id)
    except EABookingError as exc:
        reason = type(exc).__name__
        logger.warning(
            "sync_ea_calendar_pooled: EA read failed tenant=%s reason=%s msg=%s",
            tenant_id,
            reason,
            exc,
        )
        return SyncResult(ok=False, reason=reason)

    now = datetime.now(UTC)
    ea_calendar, ea_policy = build_mirror(
        settings_rows, blocked_rows, now=now, interval_s=interval_s
    )

    async with pool.acquire() as conn:
        await _write_mirror(
            conn, tenant_id=tenant_id, ea_calendar=ea_calendar, ea_policy=ea_policy,
        )

    logger.info(
        "sync_ea_calendar_pooled: tenant=%s lead=%sh tz=%s blocked=%d next_refresh=%s",
        tenant_id,
        ea_calendar.get("lead_time_hours"),
        ea_calendar.get("timezone"),
        len(ea_calendar.get("blocked_periods") or []),
        ea_calendar.get("next_refresh_at"),
    )
    return SyncResult(
        ok=True,
        synced_at=ea_calendar["synced_at"],
        next_refresh_at=ea_calendar["next_refresh_at"],
    )


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_S",
    "EA_OWNED_BOOKING_CONFIG_KEYS",
    "SyncResult",
    "build_mirror",
    "sync_ea_calendar",
    "sync_ea_calendar_pooled",
]
