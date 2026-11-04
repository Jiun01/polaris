"""recovery.py split — deferred HTTP post + marker clear (audit fix A4,
2026-07-02). Confirms check_pending_recovery is DB-only (no chatwoot arg) and
post_recovery_note_deferred does the HTTP post + clear on success, and
LEAVES the marker on failure (same contract as the pre-split function)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeConn:
    def __init__(self, row: dict | None):
        self._row = row
        self.executed: list[str] = []

    async def fetchrow(self, sql, *args):
        return self._row

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "OK"


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


async def test_check_pending_recovery_returns_none_when_no_marker() -> None:
    from polaris_api.services.chatwoot import recovery

    conn = _FakeConn(row=None)
    result = await recovery.check_pending_recovery(
        conn=conn, tenant_id=uuid4(), conversation_id=uuid4(),
    )
    assert result is None


async def test_check_pending_recovery_returns_none_when_recovery_at_null() -> None:
    from polaris_api.services.chatwoot import recovery

    conn = _FakeConn(row={
        "recovery_at": None,
        "recovery_reason": None,
        "chatwoot_conversation_id": 42,
        "chatwoot_account_id": 7,
    })
    result = await recovery.check_pending_recovery(
        conn=conn, tenant_id=uuid4(), conversation_id=uuid4(),
    )
    assert result is None


async def test_check_pending_recovery_returns_none_when_chatwoot_link_missing() -> None:
    from polaris_api.services.chatwoot import recovery

    conn = _FakeConn(row={
        "recovery_at": datetime.now(UTC),
        "recovery_reason": "chatwoot_unreachable",
        "chatwoot_conversation_id": None,
        "chatwoot_account_id": None,
    })
    result = await recovery.check_pending_recovery(
        conn=conn, tenant_id=uuid4(), conversation_id=uuid4(),
    )
    assert result is None


async def test_check_pending_recovery_returns_payload_when_pending() -> None:
    from polaris_api.services.chatwoot import recovery

    conn = _FakeConn(row={
        "recovery_at": datetime.now(UTC),
        "recovery_reason": "chatwoot_unreachable",
        "chatwoot_conversation_id": 42,
        "chatwoot_account_id": 7,
    })
    result = await recovery.check_pending_recovery(
        conn=conn, tenant_id=uuid4(), conversation_id=uuid4(),
    )
    assert result is not None
    assert result.chatwoot_account_id == 7
    assert result.chatwoot_conversation_id == 42


async def test_post_recovery_note_deferred_clears_marker_on_success() -> None:
    from polaris_api.services.chatwoot import recovery

    pending = recovery.PendingRecovery(
        chatwoot_account_id=7, chatwoot_conversation_id=42, note="test note",
    )
    conn = _FakeConn(row=None)
    pool = _FakePool(conn)
    chatwoot = AsyncMock()

    await recovery.post_recovery_note_deferred(
        pending=pending, tenant_id=uuid4(), conversation_id=uuid4(),
        chatwoot=chatwoot, pool=pool,
    )
    chatwoot.post_private_note.assert_awaited_once_with(
        account_id=7, conversation_id=42, content="test note",
    )
    assert any("UPDATE conversations" in sql for sql in conn.executed)


async def test_post_recovery_note_deferred_leaves_marker_on_chatwoot_error() -> None:
    from polaris_api.services.chatwoot import recovery
    from polaris_api.services.chatwoot.client import ChatwootError

    pending = recovery.PendingRecovery(
        chatwoot_account_id=7, chatwoot_conversation_id=42, note="test note",
    )
    conn = _FakeConn(row=None)
    pool = _FakePool(conn)
    chatwoot = AsyncMock()
    chatwoot.post_private_note.side_effect = ChatwootError("still down")

    await recovery.post_recovery_note_deferred(
        pending=pending, tenant_id=uuid4(), conversation_id=uuid4(),
        chatwoot=chatwoot, pool=pool,
    )
    assert conn.executed == []  # marker NOT cleared — next inbound retries


async def test_post_recovery_note_deferred_noop_when_chatwoot_none() -> None:
    from polaris_api.services.chatwoot import recovery

    pending = recovery.PendingRecovery(
        chatwoot_account_id=7, chatwoot_conversation_id=42, note="test note",
    )
    conn = _FakeConn(row=None)
    pool = _FakePool(conn)

    await recovery.post_recovery_note_deferred(
        pending=pending, tenant_id=uuid4(), conversation_id=uuid4(),
        chatwoot=None, pool=pool,
    )
    assert conn.executed == []
