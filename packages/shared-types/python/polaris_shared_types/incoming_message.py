"""Normalised inbound webhook event — Pydantic SSOT for Phase 2 PR-4 + PR-7.

``IncomingMessage`` is what the webhook dispatcher emits onto an
in-process async queue once an inbound delivery is HMAC-verified,
tenant-resolved, status-gated (PROCEED), and idempotency-claimed.
Phase 4's LLM consumer reads this shape; Phase 7's handoff routing
reads the same shape; Phase 6's outbound broadcast worker can mirror
it as the input format for reply correlation.

Provider quirks intentionally normalise here so consumers don't carry
per-channel branching:

  - WhatsApp delivers ``messages[].id`` (the wamid) and ``messages[].text.body``;
    timestamp is UNIX seconds as a string. Media messages carry the
    type-specific blob (``messages[].image``, ``.audio``, etc.) with the
    user's caption (when present) at ``.caption``. Reply messages carry
    ``messages[].context.id`` (the wamid being replied to).
  - Instagram + Messenger deliver ``messaging[].message.mid`` (the equivalent
    id) and ``messaging[].message.text``; timestamp is UNIX milliseconds.
    Attachments at ``message.attachments[]`` with direct URLs.
  - Chatwoot delivers ``id`` (numeric) and ``content``; ``created_at`` ISO 8601.
    Attachments at ``attachments[]`` with ``data_url``.

``provider_message_id`` carries whichever native id Meta/Chatwoot used,
stringified. Idempotency dedup keys against ``(provider, provider_message_id)``.

Field bounds are deliberately generous on the upper end (10k chars for
body) to handle WA's 4096 + IG's 1000 + Messenger's 2000 limits with
headroom for future media-caption edge cases. Pydantic 2 rejects
anything past the cap at parse time so a runaway payload can't OOM
the queue.

PR-7 additions:
  - ``in_reply_to`` — provider's reply-target message id when present
    (WA ``context.id``; IG/Messenger ``reply_to.mid``; Chatwoot
    ``in_reply_to``). Phase 4 LLM uses this to thread context;
    Phase 5 flow runtime correlates branch responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Provider = Literal[
    "whatsapp", "messenger", "chatwoot", "telegram", "instagram_login"
]


class SenderRef(BaseModel):
    """Channel-native sender identity.

    NOT a Polaris ``tenant_users.user_id`` — that's the operator side.
    ``channel_user_id`` is whatever the provider hands us:
      - WA:        E.164 phone number ('60123456789')
      - IG / FB:   the platform's user / page-scoped id
      - Chatwoot:  the contact id (stringified)
      - Telegram:  the chat id (stringified numeric; stable per user<->bot)

    ``display_name`` is best-effort and may be ``None`` on first contact
    (WA doesn't include profile name until the recipient has saved the
    business in their contacts).
    """

    channel_user_id: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=200)


class Attachment(BaseModel):
    """One media item attached to an inbound message.

    Phase 4's LLM consumer decides whether to download (Meta-hosted
    media URLs have short TTLs and need a server-side fetch with the
    Graph API token; IG and Messenger return permanent URLs in the
    webhook itself; Chatwoot returns CDN URLs).
    """

    kind: Literal["image", "audio", "video", "document", "sticker"]
    url: str | None = Field(default=None, max_length=2048)
    media_id: str | None = Field(default=None, max_length=128)
    mime_type: str | None = Field(default=None, max_length=100)


class IncomingMessage(BaseModel):
    """Normalised inbound event emitted by the webhook dispatcher.

    Consumer contract:
      - The same shape is enqueued whether the source is Meta (WA / IG /
        Messenger) or Chatwoot. Per-channel quirks live in the
        dispatcher, NOT here.
      - ``received_at`` is the Polaris-server receive time; use that for
        Polaris-side ordering. ``provider_sent_at`` is the
        provider-claimed send time when present and is useful for
        latency measurements + log correlation, but should not gate
        application logic (provider clocks drift).
      - ``body`` may be ``None`` for media-only messages; consumers
        must handle the ``attachments`` list independently. For WA
        media-with-caption messages the caption lands in ``body``
        (caption is conversational intent; treating it specially would
        force every consumer to merge two text sources).
      - ``in_reply_to`` is the provider-side id of the message being
        replied to when the user used a reply / quote feature.
        ``None`` for the common case (new message, not a reply).
    """

    tenant_id: UUID
    channel_id: UUID  # tenant_channels.id — the SPECIFIC row, ADR-015 multiplicity
    provider: Provider
    provider_message_id: str = Field(min_length=1, max_length=200)
    sender: SenderRef
    body: str | None = Field(default=None, max_length=10_000)
    attachments: list[Attachment] = Field(default_factory=list)
    received_at: datetime
    provider_sent_at: datetime | None = None
    in_reply_to: str | None = Field(default=None, max_length=200)
    # Chatwoot-native ingress: the agent-bot webhook already carries the
    # existing Chatwoot conversation + account ids. The pipeline links these
    # onto the Polaris ``conversations`` row (Chatwoot owns conversation
    # creation now) so the handoff / label / reply-mirror posts target the
    # real conversation instead of creating a duplicate. ``None`` for any
    # non-Chatwoot ingress.
    chatwoot_conversation_id: int | None = None
    chatwoot_account_id: int | None = None
