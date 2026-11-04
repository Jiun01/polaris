"""Tests for the EA → Polaris booking-settings mirror (settings_sync) and the
read-only-enforcement / sync endpoints on the tenant-bookings router.

Pure unit tests: a fake EA client + a fake asyncpg connection. No DB, no HTTP.
Covers the mapping (min→hours collapse, working-plan parse, blocked periods),
the never-raise contract, the bot-only-keys-preserved invariant, and the
router's server-side read-only enforcement of EA-owned fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from polaris_api.routers import tenant_bookings
from polaris_api.routers.tenant_bookings import (
    BookingConfig,
    get_config,
    put_config,
    sync_config,
)
from polaris_api.services.booking.client import EABookingTransientError
from polaris_api.services.booking.settings_sync import (
    SyncResult,
    build_mirror,
    sync_ea_calendar,
)

_WORKING_PLAN = (
    '{"sunday":null,"monday":{"start":"09:00","end":"18:00",'
    '"breaks":[{"start":"13:00","end":"14:00"}]},"saturday":null}'
)

_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


def _settings_rows(**overrides: Any) -> list[dict[str, Any]]:
    base = {
        "book_advance_timeout": "1440",
        "default_timezone": "Asia/Kuala_Lumpur",
        "company_working_plan": _WORKING_PLAN,
        "future_booking_limit": "90",
    }
    base.update(overrides)
    return [{"name": k, "value": v} for k, v in base.items()]


class _FakeConn:
    """fetchrow returns a fixed settings row; execute captures (sql, args).

    audit Batch B (B7-a): ``sync_ea_calendar`` now issues TWO different
    fetchrow queries — phase 1 (``_read_ea_state``) reads the full
    ``settings`` column, phase 3 (``_write_mirror``) re-reads just
    ``settings->'booking_config'`` on (possibly) a different conn. Route by
    SQL fragment so both phases see the fixture's settings blob.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any] | None:
        if self._settings is _MISSING:
            return None
        if "booking_config" in sql:
            parsed = self._settings
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except json.JSONDecodeError:
                    parsed = {}
            bc = parsed.get("booking_config") if isinstance(parsed, dict) else None
            return {"booking_config": bc}
        return {"settings": self._settings}

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"


_MISSING = object()


class _FakeEA:
    def __init__(self, *, settings_rows=None, blocked_rows=None, raises=None):
        self._settings = settings_rows if settings_rows is not None else _settings_rows()
        self._blocked = blocked_rows if blocked_rows is not None else []
        self._raises = raises

    async def get_settings(self, *, tenant_id):  # noqa: ANN001
        if self._raises:
            raise self._raises
        return self._settings

    async def list_blocked_periods(self, *, tenant_id):  # noqa: ANN001
        if self._raises:
            raise self._raises
        return self._blocked


# --- build_mirror (pure mapping) -------------------------------------------


def test_build_mirror_collapses_lead_time_and_maps_fields() -> None:
    blocked = [
        {"id": 1, "name": "Holiday", "start": "2026-12-25 00:00:00",
         "end": "2026-12-25 23:59:00", "notes": "closed"}
    ]
    ea_calendar, ea_policy = build_mirror(
        _settings_rows(), blocked, now=_NOW, interval_s=3600
    )
    # 1440 min → 24h, collapsed onto BOTH lead-time fields.
    assert ea_policy["cancellation_lead_hours"] == 24
    assert ea_policy["reschedule_window_hours"] == 24
    assert ea_policy["tenant_timezone"] == "Asia/Kuala_Lumpur"
    assert ea_calendar["lead_time_hours"] == 24
    assert ea_calendar["timezone"] == "Asia/Kuala_Lumpur"
    assert ea_calendar["future_booking_limit_days"] == 90
    assert ea_calendar["working_plan"]["monday"]["start"] == "09:00"
    assert ea_calendar["working_plan"]["sunday"] is None
    assert ea_calendar["blocked_periods"] == [
        {"start": "2026-12-25 00:00:00", "end": "2026-12-25 23:59:00",
         "notes": "closed", "name": "Holiday"}
    ]
    assert ea_calendar["synced_at"] == "2026-06-24T12:00:00+00:00"
    assert ea_calendar["next_refresh_at"] == "2026-06-24T13:00:00+00:00"
    assert ea_calendar["refresh_interval_s"] == 3600


def test_build_mirror_malformed_working_plan_is_none() -> None:
    rows = _settings_rows(company_working_plan="{not json")
    ea_calendar, _ = build_mirror(rows, [], now=_NOW, interval_s=3600)
    assert ea_calendar["working_plan"] is None


def test_build_mirror_missing_lead_time_omits_policy_keys() -> None:
    rows = [r for r in _settings_rows() if r["name"] != "book_advance_timeout"]
    _, ea_policy = build_mirror(rows, [], now=_NOW, interval_s=3600)
    assert "cancellation_lead_hours" not in ea_policy
    assert "reschedule_window_hours" not in ea_policy


def test_build_mirror_clamps_excessive_lead_time() -> None:
    rows = _settings_rows(book_advance_timeout="999999")  # ~16666h
    _, ea_policy = build_mirror(rows, [], now=_NOW, interval_s=3600)
    assert ea_policy["cancellation_lead_hours"] == 168  # capped at BookingConfig le


def test_build_mirror_sub_hour_lead_time_rounds_up_not_to_zero() -> None:
    # EA's default book_advance_timeout is 30 min. round(0.5)==0 in Python would
    # silently drop the cancel window; ceil keeps it at 1h.
    _, ea_policy30 = build_mirror(
        _settings_rows(book_advance_timeout="30"), [], now=_NOW, interval_s=3600
    )
    assert ea_policy30["cancellation_lead_hours"] == 1
    _, ea_policy90 = build_mirror(
        _settings_rows(book_advance_timeout="90"), [], now=_NOW, interval_s=3600
    )
    assert ea_policy90["cancellation_lead_hours"] == 2  # 1.5h → 2h (never under-enforce)


def test_build_mirror_zero_lead_time_stays_zero() -> None:
    # A genuine 0 (no lead time) must map to 0, not be dropped as "missing".
    _, ea_policy = build_mirror(
        _settings_rows(book_advance_timeout="0"), [], now=_NOW, interval_s=3600
    )
    assert ea_policy["cancellation_lead_hours"] == 0
    assert ea_policy["reschedule_window_hours"] == 0


# --- sync_ea_calendar (read-modify-write) ----------------------------------


async def test_sync_preserves_bot_only_keys_and_overlays_policy() -> None:
    conn = _FakeConn(
        {
            "ea_account": {"status": "active"},
            "booking_config": {
                "confirmation_message": "See you soon!",
                "cancellation_lead_hours": 2,  # stale Polaris value, EA wins
            },
        }
    )
    result = await sync_ea_calendar(conn, tenant_id=uuid4(), ea_client=_FakeEA())
    assert result.ok is True
    assert len(conn.executed) == 1
    _, args = conn.executed[0]
    ea_calendar, merged_bc = args[1], args[2]
    assert isinstance(ea_calendar, dict)
    # bot-only key survives; EA-owned lead-time overwritten to EA's 24h.
    assert merged_bc["confirmation_message"] == "See you soon!"
    assert merged_bc["cancellation_lead_hours"] == 24
    assert merged_bc["reschedule_window_hours"] == 24
    assert merged_bc["tenant_timezone"] == "Asia/Kuala_Lumpur"


async def test_sync_no_active_ea_account_is_noop() -> None:
    conn = _FakeConn({"ea_account": {"status": "deprovisioned"}})
    result = await sync_ea_calendar(conn, tenant_id=uuid4(), ea_client=_FakeEA())
    assert result.ok is False
    assert result.reason == "no_active_ea_account"
    assert conn.executed == []


async def test_sync_missing_ea_account_is_noop() -> None:
    conn = _FakeConn({"booking_config": {}})
    result = await sync_ea_calendar(conn, tenant_id=uuid4(), ea_client=_FakeEA())
    assert result.ok is False
    assert result.reason == "no_active_ea_account"
    assert conn.executed == []


async def test_sync_tenant_not_found_is_noop() -> None:
    conn = _FakeConn(_MISSING)
    result = await sync_ea_calendar(conn, tenant_id=uuid4(), ea_client=_FakeEA())
    assert result.ok is False
    assert result.reason == "tenant_not_found"
    assert conn.executed == []


async def test_sync_ea_down_is_graceful_no_write() -> None:
    conn = _FakeConn({"ea_account": {"status": "active"}})
    ea = _FakeEA(raises=EABookingTransientError("EA 502"))
    result = await sync_ea_calendar(conn, tenant_id=uuid4(), ea_client=ea)
    assert result.ok is False
    assert result.reason == "EABookingTransientError"
    assert conn.executed == []


async def test_sync_handles_string_encoded_settings() -> None:
    # asyncpg may hand settings back as a JSON string.
    conn = _FakeConn('{"ea_account": {"status": "active"}, "booking_config": {}}')
    result = await sync_ea_calendar(conn, tenant_id=uuid4(), ea_client=_FakeEA())
    assert result.ok is True
    assert len(conn.executed) == 1


# --- router: read-only enforcement + sync endpoint -------------------------


class _Ctx:
    def __init__(self) -> None:
        self.tenant_id = uuid4()


async def test_get_config_includes_ea_calendar() -> None:
    conn = _FakeConn(
        {
            "booking_config": {"confirmation_message": "hi"},
            "ea_calendar": {
                "lead_time_hours": 24,
                "timezone": "Asia/Kuala_Lumpur",
                "blocked_periods": [],
                "next_refresh_at": "2026-06-24T13:00:00+00:00",
            },
        }
    )
    view = await get_config(_Ctx(), conn)  # type: ignore[arg-type]
    assert view.confirmation_message == "hi"
    assert view.ea_calendar is not None
    assert view.ea_calendar.lead_time_hours == 24
    assert view.ea_calendar.next_refresh_at == "2026-06-24T13:00:00+00:00"


async def test_put_config_preserves_ea_owned_keys() -> None:
    # Stored config has EA-synced lead-time 24h + tz; a stale client PUTs 1h.
    conn = _FakeConn(
        {
            "booking_config": {
                "cancellation_lead_hours": 24,
                "reschedule_window_hours": 24,
                "tenant_timezone": "Asia/Kuala_Lumpur",
                "confirmation_message": "old",
            }
        }
    )
    body = BookingConfig(
        cancellation_lead_hours=1,
        reschedule_window_hours=1,
        tenant_timezone="UTC",
        confirmation_message="new message",
    )
    view = await put_config(body, _Ctx(), conn)  # type: ignore[arg-type]
    # The write payload kept EA-owned values, accepted the bot-only edit.
    _, args = conn.executed[0]
    payload = args[0]
    assert payload["cancellation_lead_hours"] == 24  # NOT 1
    assert payload["reschedule_window_hours"] == 24  # NOT 1
    assert payload["tenant_timezone"] == "Asia/Kuala_Lumpur"  # NOT UTC
    assert payload["confirmation_message"] == "new message"
    # Response reflects the same enforcement.
    assert view.cancellation_lead_hours == 24
    assert view.confirmation_message == "new message"


async def test_sync_config_endpoint_runs_sync_and_returns_view(monkeypatch) -> None:
    calls: list[Any] = []

    async def _fake_sync(conn, *, tenant_id, interval_s):  # noqa: ANN001
        calls.append((tenant_id, interval_s))
        return SyncResult(ok=True, synced_at="t", next_refresh_at="t2")

    monkeypatch.setattr(tenant_bookings, "sync_ea_calendar", _fake_sync)
    conn = _FakeConn(
        {
            "booking_config": {},
            "ea_calendar": {"lead_time_hours": 24, "blocked_periods": []},
        }
    )
    view = await sync_config(_Ctx(), conn)  # type: ignore[arg-type]
    assert len(calls) == 1
    assert view.ea_calendar is not None
    assert view.ea_calendar.lead_time_hours == 24
