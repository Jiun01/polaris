"""Telegram channel provisioning orchestrator — off-Meta channels M2
(Chatwoot-native cutover).

Wraps the pure-DB ``provision_channel`` with the Telegram-specific network
validation + secret lifecycle the admin connect flow needs:

  1. ``getMe``        — confirm the pasted bot token is live; capture bot id +
                        username (stored for display / deep-links).
  2. ``provision_channel`` — insert the tenant_channels telegram row (tier-gated
                        via allowed_channel_kinds — Pro+; chatwoot_inbox_id
                        supplied by the admin, sales-led per the PRD).
  3. ``secrets.put``  — encrypt + store the bot token (kind telegram_bot_token).

Chatwoot-native cutover: Telegram is now a native Chatwoot ``Channel::Telegram``
inbox — CHATWOOT owns the bot webhook (it ``setWebhook``s the bot to Chatwoot's
own URL). Polaris therefore NO LONGER calls ``setWebhook`` (Telegram allows ONE
webhook URL per bot; pointing it at Polaris would override Chatwoot's and break
native inbound) and stores NO ``telegram_webhook_secret`` (that secret only ever
backed Polaris's own ``/v1/webhooks/telegram/{secret}`` route, now retired —
inbound arrives via the agent-bot path, ``resolve_chatwoot`` → channel_kind
'telegram', like the Meta surfaces). Polaris keeps the bot TOKEN so it can send
bot replies DIRECT via the Bot API + mirror them into Chatwoot with a
``source_id`` (the same reply-direct-then-mirror contract the Meta surfaces use).

The getMe network call happens BEFORE the inserts so a bad token / unreachable
Telegram aborts without leaving a half-provisioned row; the row + token then
commit atomically inside the caller's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from polaris_api.services import secrets as secrets_service
from polaris_api.services.channels.provision import (
    ChannelProvisionResult,
    provision_channel,
)
from polaris_api.services.telegram_client import TelegramClient

_TELEGRAM_TOKEN_SECRET_KIND = "telegram_bot_token"  # noqa: S105 — secret KIND label, not a value


@dataclass(slots=True)
class TelegramValidation:
    """Result of the NETWORK phase (getMe) — no DB writes yet."""

    bot_id: str
    bot_username: str | None


@dataclass(slots=True)
class TelegramProvisionResult:
    channel_id: UUID
    bot_id: str
    bot_username: str | None


async def validate_telegram_bot(
    telegram_client: TelegramClient,
    *,
    bot_token: str,
) -> TelegramValidation:
    """NETWORK phase: validate the bot token via ``getMe`` + capture identity.

    Call this BEFORE opening the DB transaction so a ~20s round trip to
    api.telegram.org never holds a pooled connection / open tx. Returns the bot
    id + username (stored for display / deep-links).

    Chatwoot-native cutover: this NO LONGER calls ``setWebhook``. Chatwoot owns
    the Telegram bot webhook now (a native ``Channel::Telegram`` inbox), so
    Polaris must not point the bot at its own webhook — Telegram allows a single
    webhook URL per bot and doing so would override Chatwoot's, re-breaking
    native inbound.

    Raises ``TelegramError`` on a bad/unauthorized token (nothing persisted —
    there are no DB writes here).
    """
    me = await telegram_client.get_me(token=bot_token)
    return TelegramValidation(bot_id=str(me["id"]), bot_username=me.get("username"))


async def persist_telegram_channel(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    bot_token: str,
    validation: TelegramValidation,
    chatwoot_inbox_id: int,
    bypass_tier_cap: bool = False,
) -> TelegramProvisionResult:
    """DB phase: insert the channel row (tier-gated) + store the encrypted token.

    Call INSIDE the caller's transaction (only DB work — no network). Takes the
    pre-validated ``validation`` from ``validate_telegram_bot``.

    Chatwoot-native cutover: ``telegram_webhook_secret`` is left NULL — Polaris
    no longer owns the bot webhook (Chatwoot does), so there is no Polaris
    webhook URL / secret_token to store. The column stays nullable (its partial
    unique index is ``WHERE telegram_webhook_secret IS NOT NULL``, and the
    charset CHECK passes on NULL).

    Raises:
      ``TierCapError``  — telegram not in the tenant's allowed_channel_kinds.
      ``asyncpg.UniqueViolationError`` — chatwoot_inbox_id already in use.
    """
    result: ChannelProvisionResult = await provision_channel(
        conn,
        tenant_id=tenant_id,
        channel_kind="telegram",
        connection_payload={
            "chatwoot_inbox_id": chatwoot_inbox_id,
            "telegram_bot_id": validation.bot_id,
            "telegram_bot_username": validation.bot_username,
            # telegram_webhook_secret intentionally omitted (→ NULL): Chatwoot
            # owns the bot webhook now; Polaris stores no webhook secret.
        },
        bypass_tier_cap=bypass_tier_cap,
    )
    await secrets_service.put(
        conn, tenant_id, _TELEGRAM_TOKEN_SECRET_KIND, bot_token.encode("utf-8")
    )
    return TelegramProvisionResult(
        channel_id=result.channel_id,
        bot_id=validation.bot_id,
        bot_username=validation.bot_username,
    )
