"""seed_email_channel — agent-bot attach behavior (DB-free).

The seed script previously inserted the tenant_channels row but never attached the
Polaris Bot to the inbox — leaving email inboxes webhook-silent (Chatwoot fires
``message_created`` only for inboxes the bot is on), so no draft was ever produced.
These tests pin the new best-effort ``_attach_bot`` helper: it attaches when an
account id is known, and NEVER raises (a Chatwoot outage must not fail the seed).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apps.api.scripts import seed_email_channel as seed  # type: ignore[import-not-found]


@pytest.mark.asyncio
async def test_attach_skipped_when_no_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attach = AsyncMock()
    monkeypatch.setattr(seed, "attach_inbox_agent_bot", attach)
    monkeypatch.setattr(seed, "get_chatwoot_client", lambda: object())

    await seed._attach_bot(account_id=None, inbox_id=6)

    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_called_with_account_and_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attach = AsyncMock(return_value=True)
    client = object()
    monkeypatch.setattr(seed, "attach_inbox_agent_bot", attach)
    monkeypatch.setattr(seed, "get_chatwoot_client", lambda: client)

    await seed._attach_bot(account_id=1, inbox_id=6)

    attach.assert_awaited_once()
    assert attach.await_args.args[0] is client
    assert attach.await_args.kwargs["account_id"] == 1
    assert attach.await_args.kwargs["inbox_id"] == 6


@pytest.mark.asyncio
async def test_attach_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed, "get_chatwoot_client", lambda: object())
    monkeypatch.setattr(
        seed,
        "attach_inbox_agent_bot",
        AsyncMock(side_effect=RuntimeError("chatwoot down")),
    )

    # Best-effort: must NOT raise even when the client/attach blows up.
    await seed._attach_bot(account_id=1, inbox_id=6)
