"""reconcile_channels — periodic self-heal for unattached agent-bots +
missing channel tokens (silent-failure audit fix A5, 2026-07-02)."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeConn:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return self._rows

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        return None


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *a: object) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


async def test_reconcile_attaches_bot_and_flags_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.scheduled import reconcile_channels

    tenant_id = uuid4()
    rows = [
        {"tenant_id": tenant_id, "account_id": 1, "inbox_id": 100, "channel": "whatsapp"},
    ]
    conn = _FakeConn(rows=rows)
    pool = _FakePool(conn)

    monkeypatch.setattr(
        reconcile_channels, "resolve_platform_bot_id", AsyncMock(return_value=42)
    )
    monkeypatch.setattr(
        reconcile_channels, "attach_inbox_agent_bot", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        reconcile_channels.secrets_svc, "get", AsyncMock(return_value=None)
    )
    fixes: list[tuple] = []
    monkeypatch.setattr(
        reconcile_channels.metrics, "record_channel_reconcile_fix",
        lambda **kw: fixes.append((kw["tenant_id"], kw["kind"])),
    )
    monkeypatch.setattr(reconcile_channels, "get_chatwoot_client", lambda: object())

    await reconcile_channels._reconcile_job(pool)

    reconcile_channels.attach_inbox_agent_bot.assert_awaited_once()
    assert reconcile_channels.attach_inbox_agent_bot.await_args.kwargs["bot_id"] == 42
    assert (tenant_id, "token_missing") in fixes


async def test_reconcile_skips_when_no_active_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.scheduled import reconcile_channels

    conn = _FakeConn(rows=[])
    pool = _FakePool(conn)
    attach_mock = AsyncMock()
    monkeypatch.setattr(reconcile_channels, "attach_inbox_agent_bot", attach_mock)

    await reconcile_channels._reconcile_job(pool)
    attach_mock.assert_not_called()


async def test_reconcile_records_fix_when_bot_attach_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed agent-bot attach must record a metric, not raise."""
    from polaris_api.scheduled import reconcile_channels

    tenant_id = uuid4()
    rows = [
        {"tenant_id": tenant_id, "account_id": 1, "inbox_id": 100, "channel": "telegram"},
    ]
    conn = _FakeConn(rows=rows)
    pool = _FakePool(conn)

    monkeypatch.setattr(
        reconcile_channels, "resolve_platform_bot_id", AsyncMock(return_value=42)
    )
    monkeypatch.setattr(
        reconcile_channels, "attach_inbox_agent_bot", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(reconcile_channels, "get_chatwoot_client", lambda: object())

    fixes: list[tuple] = []
    monkeypatch.setattr(
        reconcile_channels.metrics, "record_channel_reconcile_fix",
        lambda **kw: fixes.append((kw["tenant_id"], kw["kind"])),
    )

    await reconcile_channels._reconcile_job(pool)

    assert (tenant_id, "agent_bot_attach_failed") in fixes


async def test_reconcile_skips_token_check_for_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """telegram has no entry in _CHANNEL_TO_SECRET_KIND — the token-presence
    check must not fire (and must not call secrets_svc.get) for it."""
    from polaris_api.scheduled import reconcile_channels

    tenant_id = uuid4()
    rows = [
        {"tenant_id": tenant_id, "account_id": 1, "inbox_id": 100, "channel": "telegram"},
    ]
    conn = _FakeConn(rows=rows)
    pool = _FakePool(conn)

    monkeypatch.setattr(
        reconcile_channels, "resolve_platform_bot_id", AsyncMock(return_value=42)
    )
    monkeypatch.setattr(
        reconcile_channels, "attach_inbox_agent_bot", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(reconcile_channels, "get_chatwoot_client", lambda: object())
    get_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(reconcile_channels.secrets_svc, "get", get_mock)

    await reconcile_channels._reconcile_job(pool)

    get_mock.assert_not_called()


async def test_reconcile_resolves_bot_once_per_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-account bot_id cache: N channels on one account must trigger exactly
    ONE resolve_platform_bot_id (list_agent_bots) call per account, mirroring
    scripts/backfill_agent_bot_attach.py (review finding, 2026-07-02)."""
    from polaris_api.scheduled import reconcile_channels

    t1, t2 = uuid4(), uuid4()
    rows = [
        {"tenant_id": t1, "account_id": 1, "inbox_id": 100, "channel": "telegram"},
        {"tenant_id": t1, "account_id": 1, "inbox_id": 101, "channel": "telegram"},
        {"tenant_id": t2, "account_id": 2, "inbox_id": 200, "channel": "telegram"},
    ]
    conn = _FakeConn(rows=rows)
    pool = _FakePool(conn)

    resolve_mock = AsyncMock(return_value=42)
    attach_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(reconcile_channels, "resolve_platform_bot_id", resolve_mock)
    monkeypatch.setattr(reconcile_channels, "attach_inbox_agent_bot", attach_mock)
    monkeypatch.setattr(reconcile_channels, "get_chatwoot_client", lambda: object())

    await reconcile_channels._reconcile_job(pool)

    assert resolve_mock.await_count == 2  # once per account, not per channel
    assert attach_mock.await_count == 3  # still every inbox
    assert all(c.kwargs["bot_id"] == 42 for c in attach_mock.await_args_list)


async def test_reconcile_resolve_failure_skips_attach_records_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolve failure (ChatwootError) must not raise; every inbox on that
    account counts as attach-failed and attach is never awaited."""
    from polaris_api.scheduled import reconcile_channels
    from polaris_api.services.chatwoot.client import ChatwootTransientError

    tenant_id = uuid4()
    rows = [
        {"tenant_id": tenant_id, "account_id": 1, "inbox_id": 100, "channel": "telegram"},
        {"tenant_id": tenant_id, "account_id": 1, "inbox_id": 101, "channel": "telegram"},
    ]
    conn = _FakeConn(rows=rows)
    pool = _FakePool(conn)

    resolve_mock = AsyncMock(side_effect=ChatwootTransientError("cw down"))
    attach_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(reconcile_channels, "resolve_platform_bot_id", resolve_mock)
    monkeypatch.setattr(reconcile_channels, "attach_inbox_agent_bot", attach_mock)
    monkeypatch.setattr(reconcile_channels, "get_chatwoot_client", lambda: object())

    fixes: list[tuple] = []
    monkeypatch.setattr(
        reconcile_channels.metrics, "record_channel_reconcile_fix",
        lambda **kw: fixes.append((kw["tenant_id"], kw["kind"])),
    )

    await reconcile_channels._reconcile_job(pool)

    assert resolve_mock.await_count == 1  # failed resolve cached for the tick
    attach_mock.assert_not_awaited()
    assert fixes.count((tenant_id, "agent_bot_attach_failed")) == 2
