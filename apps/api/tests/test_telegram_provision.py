"""Off-Meta channels M2 — unit tests for the split Telegram provisioning
(Chatwoot-native cutover).

DB-free. The provisioning is split into a NETWORK phase
(``validate_telegram_bot`` — getMe only, before any transaction) and a DB phase
(``persist_telegram_channel`` — provision_channel + secrets.put, inside the
caller's transaction). Tests isolate each.

Chatwoot-native cutover: Polaris no longer calls ``setWebhook`` (Chatwoot owns
the Telegram bot webhook) and stores no ``telegram_webhook_secret`` — these
tests assert exactly that.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.channels import telegram_provision as tp
from polaris_api.services.channels.provision import (
    ChannelProvisionResult,
    TierCapError,
)
from polaris_api.services.telegram_client import TelegramError

_TENANT = uuid4()


def _client(*, get_me_result: dict | None = None) -> AsyncMock:
    client = AsyncMock()
    client.get_me = AsyncMock(
        return_value=get_me_result or {"id": 123456789, "username": "acme_bot"}
    )
    client.set_webhook = AsyncMock(return_value=True)
    return client


# ---------- network phase: validate_telegram_bot ----------


@pytest.mark.asyncio
async def test_validate_calls_get_me_and_never_sets_webhook() -> None:
    client = _client()
    validation = await tp.validate_telegram_bot(client, bot_token="123:ABC")
    client.get_me.assert_awaited_once_with(token="123:ABC")
    assert validation.bot_id == "123456789"
    assert validation.bot_username == "acme_bot"
    # Chatwoot-native cutover: Polaris must NOT register the bot webhook —
    # Chatwoot owns it. setWebhook is never called.
    client.set_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_bad_token_raises() -> None:
    client = _client()
    client.get_me = AsyncMock(side_effect=TelegramError("unauthorized", status_code=401))
    with pytest.raises(TelegramError):
        await tp.validate_telegram_bot(client, bot_token="bad")
    client.set_webhook.assert_not_awaited()


# ---------- db phase: persist_telegram_channel ----------


@pytest.fixture
def db_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    channel_id = uuid4()
    provision = AsyncMock(
        return_value=ChannelProvisionResult(
            channel_id=channel_id, channel_kind="telegram", tenant_id=_TENANT
        )
    )
    put = AsyncMock()
    monkeypatch.setattr(tp, "provision_channel", provision)
    monkeypatch.setattr(tp.secrets_service, "put", put)
    return {"provision": provision, "put": put, "channel_id": channel_id}


def _validation() -> tp.TelegramValidation:
    return tp.TelegramValidation(bot_id="123456789", bot_username="acme_bot")


@pytest.mark.asyncio
async def test_persist_inserts_row_and_stores_token(db_mocks: dict[str, AsyncMock]) -> None:
    conn = AsyncMock()
    result = await tp.persist_telegram_channel(
        conn,
        tenant_id=_TENANT,
        bot_token="123:ABC",
        validation=_validation(),
        chatwoot_inbox_id=900_001,
    )
    payload = db_mocks["provision"].await_args.kwargs["connection_payload"]
    assert db_mocks["provision"].await_args.kwargs["channel_kind"] == "telegram"
    assert payload["telegram_bot_id"] == "123456789"
    assert payload["telegram_bot_username"] == "acme_bot"
    assert payload["chatwoot_inbox_id"] == 900_001
    # Chatwoot-native cutover: no Polaris webhook secret is persisted (Chatwoot
    # owns the webhook). The column is left NULL — i.e. not present in the
    # provision payload.
    assert "telegram_webhook_secret" not in payload

    put_args = db_mocks["put"].await_args
    assert put_args.args[2] == "telegram_bot_token"
    assert put_args.args[3] == b"123:ABC"

    assert result.channel_id == db_mocks["channel_id"]
    assert result.bot_username == "acme_bot"


@pytest.mark.asyncio
async def test_persist_tier_cap_does_not_store_token(db_mocks: dict[str, AsyncMock]) -> None:
    db_mocks["provision"].side_effect = TierCapError("telegram not allowed on starter")
    conn = AsyncMock()
    with pytest.raises(TierCapError):
        await tp.persist_telegram_channel(
            conn, tenant_id=_TENANT, bot_token="123:ABC",
            validation=_validation(), chatwoot_inbox_id=1,
        )
    db_mocks["put"].assert_not_awaited()
