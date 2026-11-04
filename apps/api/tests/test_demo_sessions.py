"""Unit tests for demo_sessions primitives — Demo System v3 (ADR-066).

Pattern: stub the BYPASSRLS service pool with a fake pool/conn (mirrors the
AsyncMock style in test_quota.py) so the SQL-shaping + return mapping is tested
without a live DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from polaris_api.services import demo_sessions as ds

pytestmark = pytest.mark.asyncio(loop_scope="function")

_BRAND = UUID("11111111-1111-1111-1111-111111111111")


class _FakeAcquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakePool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _patch_pool(monkeypatch: pytest.MonkeyPatch, conn: Any) -> None:
    monkeypatch.setattr(ds, "get_service_pool", lambda: _FakePool(conn))


# --- find_demo_tenant_by_code ------------------------------------------------

async def test_find_demo_tenant_by_code_match(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": _BRAND,
        "slug": "trg-watch",
        "display_name": "TRG Watch",
        "demo_code": "trgwatches1824",
        "demo_duration_hours": 6,
        "demo_max_messages": 50,
    }
    _patch_pool(monkeypatch, conn)

    demo = await ds.find_demo_tenant_by_code("TRGWATCHES1824")  # case-insensitive

    assert demo is not None
    assert demo.tenant_id == _BRAND
    assert demo.display_name == "TRG Watch"
    assert demo.duration_hours == 6
    assert demo.max_messages == 50


async def test_find_demo_tenant_by_code_blank_skips_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    _patch_pool(monkeypatch, conn)

    assert await ds.find_demo_tenant_by_code("   ") is None
    conn.fetchrow.assert_not_called()


async def test_find_demo_tenant_by_code_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    _patch_pool(monkeypatch, conn)

    assert await ds.find_demo_tenant_by_code("nope0000") is None


# --- has_ever_used_demo (one-ever lock) --------------------------------------

async def test_has_ever_used_demo_true(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = 1
    _patch_pool(monkeypatch, conn)
    assert await ds.has_ever_used_demo("+60123") is True


async def test_has_ever_used_demo_false(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = None
    _patch_pool(monkeypatch, conn)
    assert await ds.has_ever_used_demo("+60123") is False


# --- get_active_demo_session -------------------------------------------------

async def test_get_active_demo_session_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": sid,
        "tenant_id": _BRAND,
        "demo_code": "trgwatches1824",
        "message_count": 3,
        "max_messages": 50,
        "expires_at": datetime.now(UTC) + timedelta(hours=5),
        "chatwoot_conversation_id": 42,
        "chatwoot_inbox_id": 7,
    }
    _patch_pool(monkeypatch, conn)

    sess = await ds.get_active_demo_session("+60123")
    assert sess is not None
    assert sess.session_id == sid
    assert sess.tenant_id == _BRAND
    assert sess.message_count == 3
    assert sess.chatwoot_conversation_id == 42


async def test_get_active_demo_session_none(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    _patch_pool(monkeypatch, conn)
    assert await ds.get_active_demo_session("+60123") is None


# --- activate_demo_session ---------------------------------------------------

async def test_activate_demo_session_binds_brand_and_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": sid}
    _patch_pool(monkeypatch, conn)

    demo = ds.DemoTenant(
        tenant_id=_BRAND,
        slug="trg-watch",
        display_name="TRG Watch",
        demo_code="trgwatches1824",
        duration_hours=6,
        max_messages=50,
    )
    before = datetime.now(UTC)
    sess = await ds.activate_demo_session(
        channel="whatsapp",
        channel_user_id="+60123",
        channel_user_name="Jane",
        demo=demo,
    )

    assert sess.session_id == sid
    assert sess.tenant_id == _BRAND
    assert sess.message_count == 0
    assert sess.max_messages == 50
    # expires ~ now + 6h
    assert sess.expires_at >= before + timedelta(hours=6) - timedelta(minutes=1)

    # INSERT bound tenant_id to the BRAND (first positional after the SQL string)
    args = conn.fetchrow.call_args.args
    assert args[1] == _BRAND
    assert args[5] == "trgwatches1824"  # demo_code
    assert args[6] == 50  # max_messages


async def test_increment_demo_message(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = 5
    _patch_pool(monkeypatch, conn)
    assert await ds.increment_demo_message(uuid4()) == 5


async def test_reset_visitor_demo_returns_deleted_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.execute.return_value = "DELETE 3"
    _patch_pool(monkeypatch, conn)
    assert await ds.reset_visitor_demo("+60123") == 3
