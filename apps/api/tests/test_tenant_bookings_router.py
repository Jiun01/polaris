"""Unit tests for tenant_bookings router — Phase 9 B-004.

All tests mock `get_tenant_context` and `get_request_conn` so no DB is
required. Covers:

  * GET returns defaults when tenants.settings is NULL.
  * GET deserialises a saved JSONB object correctly.
  * PUT returns the body that was sent (no round-trip to DB needed for
    return value — the router echoes the validated body).
  * PUT round-trip: mock fetchrow to return what was PUT; verify GET
    returns the same values.
  * Boundary inputs accepted (lead=0, lead=168, window=168).
  * Invalid inputs rejected with 422 (lead>168, message>2000 chars).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Prime env before polaris_api imports so settings LRU cache bakes correct values.
_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)


from polaris_api.deps.tenant import (  # noqa: E402
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.routers.tenant_bookings import router as bookings_router  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = uuid4()
_USER_ID = uuid4()


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT_ID,
        user_id=_USER_ID,
        role="admin",
        plan_tier="starter",
        status="active",
    )


def _mock_conn(settings_value: Any = None) -> AsyncMock:
    """Return a mock asyncpg.Connection whose fetchrow yields a settings row."""
    conn = AsyncMock(spec=asyncpg.Connection)
    conn.fetchrow = AsyncMock(return_value={"settings": settings_value})
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


@pytest_asyncio.fixture(loop_scope="function")
async def client_with(request: pytest.FixtureRequest) -> AsyncIterator[tuple[AsyncClient, AsyncMock]]:  # type: ignore[override]
    """Yield (AsyncClient, mock_conn). `request.param` is the settings value."""
    settings_value = getattr(request, "param", None)
    conn = _mock_conn(settings_value)

    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")

    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, conn


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_returns_defaults_when_settings_null() -> None:
    conn = _mock_conn(settings_value=None)
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/tenant/bookings/config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["cancellation_lead_hours"] == 24
    assert body["reschedule_window_hours"] == 24
    assert body["confirmation_message"] == ""
    assert body["allow_self_serve_cancellation"] is True


@pytest.mark.asyncio
async def test_get_deserialises_saved_config() -> None:
    saved = {
        "booking_config": {
            "cancellation_lead_hours": 48,
            "reschedule_window_hours": 72,
            "confirmation_message": "See you soon!",
            "allow_self_serve_cancellation": False,
        }
    }
    conn = _mock_conn(settings_value=saved)
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/tenant/bookings/config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["cancellation_lead_hours"] == 48
    assert body["reschedule_window_hours"] == 72
    assert body["confirmation_message"] == "See you soon!"
    assert body["allow_self_serve_cancellation"] is False


@pytest.mark.parametrize(
    ("saved_cfg", "expected"),
    [({"inchat_picker": True}, True), ({}, False)],
)
@pytest.mark.asyncio
async def test_get_deserialises_inchat_picker(
    saved_cfg: dict[str, Any], expected: bool
) -> None:
    """inchat_picker round-trips (true) and defaults to false when absent."""
    conn = _mock_conn(settings_value={"booking_config": saved_cfg})
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()

    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn

    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/tenant/bookings/config")

    assert resp.status_code == 200
    assert resp.json()["inchat_picker"] is expected


@pytest.mark.asyncio
async def test_get_deserialises_json_string_settings() -> None:
    """Settings stored as a JSON string (legacy path) still parses."""
    saved_str = json.dumps({"booking_config": {"cancellation_lead_hours": 12}})
    conn = _mock_conn(settings_value=saved_str)
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/tenant/bookings/config")

    assert resp.status_code == 200
    assert resp.json()["cancellation_lead_hours"] == 12


# ---------------------------------------------------------------------------
# PUT /config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_echoes_bot_fields_but_preserves_ea_owned() -> None:
    # EA is the source of truth for lead-time + timezone: the PUT re-overlays the
    # STORED values (10h / KL) server-side and ignores the client's 55/33/UTC, so
    # a read-only-rendering portal (or a stale/forged client) can't change them.
    # The bot-only messaging fields still round-trip.
    stored = {
        "booking_config": {
            "cancellation_lead_hours": 10,
            "reschedule_window_hours": 10,
            "tenant_timezone": "Asia/Kuala_Lumpur",
            "confirmation_message": "old",
        }
    }
    conn = _mock_conn(settings_value=stored)
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    payload = {
        "cancellation_lead_hours": 55,
        "reschedule_window_hours": 33,
        "tenant_timezone": "UTC",
        "confirmation_message": "See you!",
        "allow_self_serve_cancellation": False,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put("/api/tenant/bookings/config", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    # EA-owned: preserved from stored, NOT the client's 55/33/UTC.
    assert body["cancellation_lead_hours"] == 10
    assert body["reschedule_window_hours"] == 10
    assert body["tenant_timezone"] == "Asia/Kuala_Lumpur"
    # Bot-only: accepted.
    assert body["confirmation_message"] == "See you!"
    assert body["allow_self_serve_cancellation"] is False


@pytest.mark.asyncio
async def test_put_calls_db_execute() -> None:
    """PUT must call conn.execute (jsonb_set UPDATE), not just return body."""
    conn = _mock_conn()
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.put("/api/tenant/bookings/config", json={
            "cancellation_lead_hours": 24,
            "reschedule_window_hours": 24,
            "confirmation_message": "",
            "allow_self_serve_cancellation": True,
        })

    conn.execute.assert_awaited_once()
    call_args = conn.execute.await_args
    # First positional arg is the SQL; second is the JSONB payload dict.
    sql: str = call_args.args[0]
    assert "jsonb_set" in sql
    jsonb_payload: dict = call_args.args[1]  # must be a dict, not a str
    assert isinstance(jsonb_payload, dict), (
        "asyncpg requires a dict for JSONB binding — model_dump_json() "
        "would produce a str and double-encode the JSONB"
    )


# ---------------------------------------------------------------------------
# Round-trip: PUT then GET returns the same values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_get_round_trip() -> None:
    """Simulate the full round-trip: PUT values, mock GET to return same saved
    config, assert the read-back matches what was written."""
    put_payload = {
        "cancellation_lead_hours": 48,
        "reschedule_window_hours": 72,
        "confirmation_message": "Booking confirmed!",
        "allow_self_serve_cancellation": False,
    }

    # After PUT we expect this structure in tenants.settings.
    saved_settings = {"booking_config": put_payload}

    conn = _mock_conn(settings_value=saved_settings)
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        put_resp = await c.put("/api/tenant/bookings/config", json=put_payload)
        assert put_resp.status_code == 200

        get_resp = await c.get("/api/tenant/bookings/config")
        assert get_resp.status_code == 200

    # Phase 13 added reminder_offset_hours (default 24) and quiet_hours_start/
    # end (default None) to BookingConfig. The PUT payload carries the original
    # four keys; GET serialises all seven fields, so the round-trip echoes the
    # new defaults alongside what was written.
    expected = {
        **put_payload,
        "reminder_offset_hours": 24,
        "quiet_hours_start": None,
        "quiet_hours_end": None,
        # Booking-notifications feature added template-name + tz knobs
        # (all default None) to BookingConfig.
        "tenant_timezone": None,
        "confirmation_template_name": None,
        "confirmation_template_language": None,
        "reminder_template_name": None,
        "reminder_template_language": None,
        "cancellation_template_name": None,
        "cancellation_template_language": None,
        "reschedule_template_name": None,
        "reschedule_template_language": None,
        # Phase-13 booking-mode toggle (default link mode).
        "inchat_picker": False,
        # GET now returns the read-only EA mirror alongside the config; this
        # tenant has never synced, so it's null.
        "ea_calendar": None,
    }
    assert get_resp.json() == expected


# ---------------------------------------------------------------------------
# Boundary validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("lead,window", [(0, 0), (168, 168), (0, 168)])
async def test_put_boundary_values_accepted(lead: int, window: int) -> None:
    conn = _mock_conn()
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put("/api/tenant/bookings/config", json={
            "cancellation_lead_hours": lead,
            "reschedule_window_hours": window,
            "confirmation_message": "",
            "allow_self_serve_cancellation": True,
        })

    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("lead,window", [(-1, 24), (169, 24), (24, -1), (24, 169)])
async def test_put_out_of_range_returns_422(lead: int, window: int) -> None:
    conn = _mock_conn()
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put("/api/tenant/bookings/config", json={
            "cancellation_lead_hours": lead,
            "reschedule_window_hours": window,
            "confirmation_message": "",
            "allow_self_serve_cancellation": True,
        })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_message_at_max_length_accepted() -> None:
    conn = _mock_conn()
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put("/api/tenant/bookings/config", json={
            "cancellation_lead_hours": 24,
            "reschedule_window_hours": 24,
            "confirmation_message": "x" * 2000,
            "allow_self_serve_cancellation": True,
        })

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_put_message_over_limit_returns_422() -> None:
    conn = _mock_conn()
    app = FastAPI()
    app.include_router(bookings_router, prefix="/api/tenant/bookings")
    app.dependency_overrides[get_tenant_context] = lambda: _ctx()
    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn
    app.dependency_overrides[get_request_conn] = _conn_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put("/api/tenant/bookings/config", json={
            "cancellation_lead_hours": 24,
            "reschedule_window_hours": 24,
            "confirmation_message": "x" * 2001,
            "allow_self_serve_cancellation": True,
        })

    assert resp.status_code == 422
