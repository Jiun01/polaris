"""Reverse-handoff state-guard tests (B).

DB-free: a fake asyncpg pool/conn lets us drive ``process_reverse_handoff``
through its four branches —
  * conversation found + in handoff      → resume once (clear + audit + label)
  * conversation found + NOT in handoff  → clean no-op (spurious resume killed)
  * conversation not found, 1 unlinked   → race fallback reconciles + resumes
  * conversation not found, 0 / >1       → loud ERROR, no state change
without touching Postgres.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.chatwoot import reverse_handoff as rh

_HANDOFF_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    def __init__(self, *, fetchrow: Any, fetch: list[Any]) -> None:
        self._fetchrow = fetchrow
        self._fetch = fetch
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def fetchrow(self, _sql: str, *_args: Any) -> Any:
        return self._fetchrow

    async def fetch(self, _sql: str, *_args: Any) -> list[Any]:
        return self._fetch

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        return "UPDATE 1"


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _wire(
    monkeypatch: pytest.MonkeyPatch, conn: _FakeConn
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Patch pool + audit + chatwoot client; return
    (audit_record, label_apply, flip_status)."""
    monkeypatch.setattr(rh, "get_service_pool", lambda: _FakePool(conn))
    audit_record = AsyncMock()
    monkeypatch.setattr(rh.audit, "record", audit_record)
    cw_client = AsyncMock()
    # Stage-3: reverse-handoff now applies bot-handling via sync_handling_label
    # (read-modify-write). get returns [] so the sync issues a set.
    cw_client.get_conversation_labels = AsyncMock(return_value=[])
    cw_client.set_conversation_labels = AsyncMock()
    cw_client.ensure_label = AsyncMock(return_value=True)
    # Webhook-resilience: reverse-handoff also flips Chatwoot status → pending so
    # the bot re-owns the conversation (the label alone is cosmetic — the inbound
    # gate keys on status=='pending').
    cw_client.flip_conversation_status = AsyncMock()
    monkeypatch.setattr(rh, "get_chatwoot_client", lambda: cw_client)
    return (
        audit_record,
        cw_client.set_conversation_labels,
        cw_client.flip_conversation_status,
    )


async def _run() -> None:
    await rh.process_reverse_handoff(
        tenant_id=uuid4(),
        chatwoot_account_id=1,
        chatwoot_conversation_id=99,
        trigger_event="conversation_updated",
        trigger_detail="human-handling label removed",
    )


def _clear_calls(conn: _FakeConn) -> list[tuple[str, tuple[Any, ...]]]:
    return [(s, a) for (s, a) in conn.executes if "handoff_at" in s and "NULL" in s]


def _link_calls(conn: _FakeConn) -> list[tuple[str, tuple[Any, ...]]]:
    return [(s, a) for (s, a) in conn.executes if "chatwoot_conversation_id" in s]


@pytest.mark.asyncio
async def test_found_not_in_handoff_is_clean_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unrelated label change on a non-handoff conversation must NOT resume."""
    conn = _FakeConn(fetchrow={"id": uuid4(), "handoff_at": None}, fetch=[])
    audit_record, label_apply, flip_status = _wire(monkeypatch, conn)

    await _run()

    assert _clear_calls(conn) == []  # handoff columns untouched
    audit_record.assert_not_awaited()
    label_apply.assert_not_awaited()
    flip_status.assert_not_awaited()  # no spurious resume → no status flip


@pytest.mark.asyncio
async def test_found_in_handoff_resumes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing human-handling on an in-handoff conversation → resume once."""
    conn = _FakeConn(fetchrow={"id": uuid4(), "handoff_at": _HANDOFF_AT}, fetch=[])
    audit_record, label_apply, flip_status = _wire(monkeypatch, conn)

    await _run()

    # Exactly one execute (the step-2 clear) — no reconcile link-write at all.
    assert len(conn.executes) == 1
    assert len(_clear_calls(conn)) == 1
    assert _link_calls(conn) == []  # already linked — no reconcile
    audit_record.assert_awaited_once()
    label_apply.assert_awaited_once()
    assert label_apply.await_args.kwargs["labels"] == ["bot-handling"]
    # Real transition → not flagged as a race reconcile.
    assert audit_record.await_args.kwargs["after"]["reconciled_via_race"] is False
    # Status flipped back to pending so the bot re-owns the conversation.
    flip_status.assert_awaited_once()
    assert flip_status.await_args.kwargs["status"] == "pending"
    assert flip_status.await_args.kwargs["conversation_id"] == 99


@pytest.mark.asyncio
async def test_race_fallback_single_candidate_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume arrived before the link was set → reconcile the sole unlinked
    in-handoff conversation and resume."""
    conv_id = uuid4()
    conn = _FakeConn(
        fetchrow=None,
        fetch=[{"id": conv_id, "handoff_at": _HANDOFF_AT}],
    )
    audit_record, label_apply, flip_status = _wire(monkeypatch, conn)

    await _run()

    # Both the link back-fill AND the handoff clear ran.
    assert len(_link_calls(conn)) == 1
    assert len(_clear_calls(conn)) == 1
    audit_record.assert_awaited_once()
    assert audit_record.await_args.kwargs["after"]["reconciled_via_race"] is True
    label_apply.assert_awaited_once()
    # Reconciled resume still flips status back to pending.
    flip_status.assert_awaited_once()
    assert flip_status.await_args.kwargs["status"] == "pending"


@pytest.mark.asyncio
async def test_missing_row_zero_candidates_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No row + nothing in handoff → loud ERROR, no state change."""
    conn = _FakeConn(fetchrow=None, fetch=[])
    audit_record, label_apply, flip_status = _wire(monkeypatch, conn)

    with caplog.at_level(logging.ERROR, logger=rh.logger.name):
        await _run()

    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert conn.executes == []  # nothing touched
    audit_record.assert_not_awaited()
    label_apply.assert_not_awaited()
    flip_status.assert_not_awaited()  # stuck handoff → no status flip


@pytest.mark.asyncio
async def test_missing_row_multiple_candidates_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No row + ambiguous (>1 unlinked in-handoff) → loud ERROR, no action."""
    conn = _FakeConn(
        fetchrow=None,
        fetch=[
            {"id": uuid4(), "handoff_at": _HANDOFF_AT},
            {"id": uuid4(), "handoff_at": _HANDOFF_AT},
        ],
    )
    audit_record, label_apply, flip_status = _wire(monkeypatch, conn)

    with caplog.at_level(logging.ERROR, logger=rh.logger.name):
        await _run()

    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert _clear_calls(conn) == []
    assert _link_calls(conn) == []
    audit_record.assert_not_awaited()
    label_apply.assert_not_awaited()
    flip_status.assert_not_awaited()  # ambiguous → no status flip
