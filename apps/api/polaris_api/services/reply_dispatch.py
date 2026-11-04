"""Reply dispatch — Phase 4 PR-4, extended Phase 15 PR-6.

Routes a ResponseEnvelope to the correct outbound channel client and, when the
envelope carries product images, sequences text + image(s) per channel:

  * WhatsApp — native image caption: the first image carries the reply text as
    its caption (one message); extra images follow uncaptioned. No images →
    plain text send (the original Phase 4 demo path).
  * Instagram / Messenger — no native caption: send the text message first, then
    each image as a separate attachment (Phase 15 wires conversational outbound
    for these channels, which Phase 6 only did for broadcasts/templates).

This layer stays pure — channel routing + sequencing only. Per-tenant credential
resolution (Page token) and signed-read-URL minting happen at the caller
(``pipeline._dispatch_reply``) and arrive here as ``page_access_token`` +
``image_urls`` so this module needs no DB / settings access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal

from polaris_shared_types import ResponseEnvelope

from polaris_api.services.meta_messaging import (
    InstagramLoginClient,
    MetaMessagingClient,
    Platform,
)
from polaris_api.services.meta_whatsapp import MetaWhatsAppClient
from polaris_api.services.telegram_client import TelegramClient

logger = logging.getLogger(__name__)


class DispatchSkipped(Exception):
    """A reply could not be sent because the channel's outbound client/token/
    credentials are missing or unconfigured.

    This is a PERMANENT misconfiguration (operator action needed), NOT a
    transient provider outage. The pure dispatch layer has no DB / metrics
    access, so instead of silently returning ``None`` it raises this for the
    caller (``pipeline._dispatch_reply``) to record a durable failure signal +
    metric and log at ERROR. ``reason`` is a low-cardinality tag for the metric
    (e.g. 'missing_credentials', 'no_bot_token', 'no_page_token').
    """

    def __init__(self, channel: str, reason: str) -> None:
        super().__init__(f"{channel} send skipped: {reason}")
        self.channel = channel
        self.reason = reason


# Mirrors conversations.channel CHECK in migrations 026 + 094 + 104.
DispatchChannel = Literal[
    "whatsapp", "instagram", "messenger", "demo", "telegram", "instagram_login"
]

# WhatsApp Cloud API rejects text bodies above 4096 chars (M-2 audit fix).
_WHATSAPP_BODY_MAX_CHARS: Final[int] = 4096
# WhatsApp image *caption* limit is tighter than the text-body limit.
_WHATSAPP_CAPTION_MAX_CHARS: Final[int] = 1024
# Telegram Bot API limits (context7-verified): sendMessage text 1-4096;
# sendPhoto caption 0-1024. Same numbers as WA but kept distinct for clarity.
_TELEGRAM_BODY_MAX_CHARS: Final[int] = 4096
_TELEGRAM_CAPTION_MAX_CHARS: Final[int] = 1024
_TRUNCATION_MARKER: Final[str] = "… [truncated]"


def _clip(body: str, limit: int) -> tuple[str, bool]:
    """Return (clipped, was_truncated), reserving room for the marker."""
    if len(body) <= limit:
        return body, False
    budget = limit - len(_TRUNCATION_MARKER)
    return body[:budget].rstrip() + _TRUNCATION_MARKER, True


def _clip_for_whatsapp(body: str) -> tuple[str, bool]:
    """Back-compat wrapper (tests + callers): clip to the WhatsApp text-body
    limit. Retained so the Phase-4 truncation contract + its tests are stable."""
    return _clip(body, _WHATSAPP_BODY_MAX_CHARS)


@dataclass(slots=True)
class ConversationRef:
    """Minimal conversation slice the dispatcher needs."""

    channel: DispatchChannel
    channel_user_id: str


async def send_reply(
    *,
    conversation: ConversationRef,
    response: ResponseEnvelope,
    meta_client: MetaWhatsAppClient,
    messaging_client: MetaMessagingClient | None = None,
    page_access_token: str | None = None,
    whatsapp_phone_number_id: str | None = None,
    whatsapp_access_token: str | None = None,
    telegram_client: TelegramClient | None = None,
    telegram_token: str | None = None,
    instagram_login_client: InstagramLoginClient | None = None,
    ig_login_access_token: str | None = None,
    image_urls: list[str] | None = None,
) -> str | None:
    """Send the assistant reply (text + optional product images) over the
    conversation's channel. Returns the provider-side id of the first message
    sent (wamid / mid), or None when dispatch is skipped (e.g. IG/Messenger with
    no Page token configured).

    ``image_urls`` are pre-minted signed read URLs (caller mints from
    ``response.images`` storage paths). ``page_access_token`` is the per-tenant
    Page token required for IG/Messenger sends. ``whatsapp_phone_number_id`` +
    ``whatsapp_access_token`` are the per-tenant WABA credentials (resolved from
    ``tenant_channels`` / ``tenant_secrets``) used for WhatsApp sends.

    Raises ``DispatchSkipped`` (permanent misconfig) when a channel's client /
    token / credentials are missing — the caller records the failure. A
    'whatsapp' channel with unresolved creds is NOT sent (it raises instead).
    """
    images = image_urls or []

    if conversation.channel == "whatsapp":
        return await _send_whatsapp(
            to=conversation.channel_user_id,
            content=response.content,
            image_urls=images,
            meta_client=meta_client,
            phone_number_id=whatsapp_phone_number_id,
            access_token=whatsapp_access_token,
        )

    if conversation.channel == "messenger":
        platform: Platform = "messenger"
        return await _send_meta_messaging(
            platform=platform,
            to=conversation.channel_user_id,
            content=response.content,
            image_urls=images,
            messaging_client=messaging_client,
            page_access_token=page_access_token,
        )

    if conversation.channel == "telegram":
        return await _send_telegram(
            to=conversation.channel_user_id,
            content=response.content,
            image_urls=images,
            telegram_client=telegram_client,
            telegram_token=telegram_token,
        )

    if conversation.channel == "instagram_login":
        return await _send_instagram_login(
            to=conversation.channel_user_id,
            content=response.content,
            image_urls=images,
            client=instagram_login_client,
            access_token=ig_login_access_token,
        )

    raise ValueError(f"unknown channel: {conversation.channel}")


async def _send_whatsapp(
    *,
    to: str,
    content: str,
    image_urls: list[str],
    meta_client: MetaWhatsAppClient,
    phone_number_id: str | None = None,
    access_token: str | None = None,
) -> str | None:
    """Send a WhatsApp reply via the Cloud API (Phase 15 image support).

    Per-tenant routing: the send requires the channel's own ``phone_number_id``
    + ``access_token`` (resolved from ``tenant_channels`` / ``tenant_secrets`` by
    the caller — for a demo turn these are the gateway channel's creds, resolved
    by channel owner; see pipeline ``_dispatch_reply``) and is sent from that
    WABA number.

    Raises ``DispatchSkipped('whatsapp', 'missing_credentials')`` when the send
    has no resolved creds — we surface the misconfig instead of guessing a sender.
    """
    if not phone_number_id or not access_token:
        raise DispatchSkipped("whatsapp", "missing_credentials")
    pid = phone_number_id

    if not image_urls:
        body, truncated = _clip(content, _WHATSAPP_BODY_MAX_CHARS)
        if truncated:
            logger.warning(
                "reply_dispatch truncated whatsapp body to=%s orig_len=%d",
                to, len(content),
            )
        return await meta_client.send_text(
            phone_number_id=pid, to=to, body=body, access_token=access_token
        )

    # Images: first carries the reply text as caption; the rest are bare.
    caption, _ = _clip(content, _WHATSAPP_CAPTION_MAX_CHARS) if content else ("", False)
    first_wamid = await meta_client.send_image(
        phone_number_id=pid, to=to, image_link=image_urls[0],
        caption=caption or None, access_token=access_token,
    )
    for url in image_urls[1:]:
        await meta_client.send_image(
            phone_number_id=pid, to=to, image_link=url, access_token=access_token
        )
    return first_wamid


async def _send_telegram(
    *,
    to: str,
    content: str,
    image_urls: list[str],
    telegram_client: TelegramClient | None,
    telegram_token: str | None,
) -> str | None:
    """Telegram send: native photo caption (like WhatsApp). The first image
    carries the reply text as its caption; extra images follow bare. No images
    → plain text send.

    Raises ``DispatchSkipped`` when the client or per-tenant bot token isn't
    wired — a hard misconfiguration (no Telegram reply can ever go out for this
    tenant). The caller records it at ERROR + metric rather than losing it
    silently after the message row is committed."""
    if telegram_client is None:
        raise DispatchSkipped("telegram", "no_client")
    if not telegram_token:
        raise DispatchSkipped("telegram", "no_bot_token")

    if not image_urls:
        body, truncated = _clip(content, _TELEGRAM_BODY_MAX_CHARS)
        if truncated:
            logger.warning(
                "reply_dispatch truncated telegram body to=%s orig_len=%d",
                to, len(content),
            )
        return await telegram_client.send_message(
            token=telegram_token, chat_id=to, text=body
        )

    # Images: first carries the reply text as caption; the rest are bare.
    caption, _ = _clip(content, _TELEGRAM_CAPTION_MAX_CHARS) if content else ("", False)
    first_id = await telegram_client.send_photo(
        token=telegram_token, chat_id=to, photo_url=image_urls[0],
        caption=caption or None,
    )
    for url in image_urls[1:]:
        await telegram_client.send_photo(
            token=telegram_token, chat_id=to, photo_url=url
        )
    return first_id


async def _send_meta_messaging(
    *,
    platform: Platform,
    to: str,
    content: str,
    image_urls: list[str],
    messaging_client: MetaMessagingClient | None,
    page_access_token: str | None,
) -> str | None:
    """IG/Messenger conversational send: text first, then image attachment(s)
    (these channels have no native caption).

    Raises ``DispatchSkipped`` when no client is wired or the tenant has no Page
    token — a permanent misconfig the caller records (ERROR + metric) instead of
    silently dropping the reply after the message row is committed."""
    if messaging_client is None:
        raise DispatchSkipped(platform, "no_client")
    if not page_access_token:
        raise DispatchSkipped(platform, "no_page_token")

    first_mid: str | None = None
    if content:
        first_mid = await messaging_client.send_text(
            page_access_token=page_access_token,
            recipient_id=to,
            text=content,
            platform=platform,
        )
    for url in image_urls:
        mid = await messaging_client.send_image(
            page_access_token=page_access_token,
            recipient_id=to,
            image_url=url,
            platform=platform,
        )
        first_mid = first_mid or mid
    return first_mid


async def _send_instagram_login(
    *,
    to: str,
    content: str,
    image_urls: list[str],
    client: InstagramLoginClient | None,
    access_token: str | None,
) -> str | None:
    """Instagram-Login conversational send (graph.instagram.com): text first,
    then image attachment(s). Like IG/Messenger, no native caption; unlike them,
    uses the IG-Login user token and omits ``messaging_type``.

    Raises ``DispatchSkipped`` when no client is wired or the tenant has no
    IG-Login token — a permanent misconfig the caller records (ERROR + metric)
    instead of silently dropping the reply after the row is committed."""
    if client is None:
        raise DispatchSkipped("instagram_login", "no_client")
    if not access_token:
        raise DispatchSkipped("instagram_login", "no_ig_login_token")

    first_mid: str | None = None
    if content:
        first_mid = await client.send_text(
            access_token=access_token,
            recipient_id=to,
            text=content,
        )
    for url in image_urls:
        mid = await client.send_image(
            access_token=access_token,
            recipient_id=to,
            image_url=url,
        )
        first_mid = first_mid or mid
    return first_mid
