"""Tests for services/booking/config.read_booking_config (Phase A).

Fakes the asyncpg connection — no real DB. Verifies the soft-fail contract:
missing tenant / NULL / string-encoded / malformed settings all yield ``{}``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.booking.config import read_booking_config

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def test_returns_booking_config_dict_when_present() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={"settings": {"booking_config": {"reminder_offset_hours": 12}}}
    )
    result = await read_booking_config(conn, uuid4())
    assert result == {"reminder_offset_hours": 12}


async def test_parses_json_string_settings() -> None:
    """asyncpg may hand settings back as a JSON string — handle both."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={"settings": '{"booking_config": {"tenant_timezone": "Asia/Kuala_Lumpur"}}'}
    )
    result = await read_booking_config(conn, uuid4())
    assert result == {"tenant_timezone": "Asia/Kuala_Lumpur"}


async def test_missing_tenant_returns_empty() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    assert await read_booking_config(conn, uuid4()) == {}


async def test_null_settings_returns_empty() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"settings": None})
    assert await read_booking_config(conn, uuid4()) == {}


async def test_malformed_json_string_returns_empty() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"settings": "{not valid json"})
    assert await read_booking_config(conn, uuid4()) == {}


async def test_no_booking_config_key_returns_empty() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"settings": {"other_key": 1}})
    assert await read_booking_config(conn, uuid4()) == {}


async def test_booking_config_not_a_dict_returns_empty() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"settings": {"booking_config": "oops"}})
    assert await read_booking_config(conn, uuid4()) == {}


async def test_settings_not_a_dict_returns_empty() -> None:
    """settings itself being a non-dict / non-string (e.g. a stray int) must
    soft-fail to {} rather than raise."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"settings": 42})
    assert await read_booking_config(conn, uuid4()) == {}
