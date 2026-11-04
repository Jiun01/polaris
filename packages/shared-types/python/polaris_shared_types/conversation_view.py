"""Portal Conversations viewer shapes — Pydantic SSOT (Phase 8 PR-6).

Drives the read-only Conversations screen (#2) per 00a §6.13 item 2 +
portal_design_prompt.md §2. The portal never replies from here — that's
Chatwoot — so these shapes are output-only (list + detail). All
timestamps are RFC3339 UTC strings, matching the established notification
+ handoff pattern (router-level ``.isoformat()`` after asyncpg read).

Backing tables: ``conversations`` (migration 026) + ``messages``
(migration 028). RLS scopes every read on ``app.current_tenant_id``.

ConfigDict(extra='forbid') so a future column landing on either table
fails the shared-types contract instead of silently dropping out of the
portal payload.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")

ConversationChannel = Literal[
    "whatsapp", "messenger", "demo", "telegram", "instagram_login"
]
"""Mirror of the conversations.channel CHECK constraint (migrations 026 + 094 + 104 + 109)."""

MessageRole = Literal["user", "assistant", "system"]
"""Mirror of messages.role CHECK constraint in migration 028."""

MessageIntent = Literal[
    "greeting", "faq", "booking", "handoff", "promo", "other"
]
"""Mirror of messages.intent CHECK constraint in migration 028."""

MessageModelUsed = Literal["haiku", "sonnet", "gpt-4o-mini-fallback"]
"""Mirror of messages.model_used CHECK constraint in migration 028."""

MessageAuthorType = Literal["bot", "human_agent"]
"""Who authored an ``assistant``-role message.

Both bot replies and a human agent's Chatwoot reply (mirrored into ``messages``
post-2026-06-13 cutover, see ``services/chatwoot/agent_reply.py``) carry
``role='assistant'`` — the distinction lives in ``messages.metadata.author_type``
so the portal renders a human reply distinctly from a bot reply. Defaults to
``'bot'`` for every pre-existing row (empty metadata)."""


class ConversationListItem(BaseModel):
    """One row in the Conversations table.

    ``customer_display_name`` is pulled from ``conversations.metadata->>
    'display_name'`` when present (the pipeline writes it from the
    inbound webhook payload); when absent we fall back to a redacted
    form of ``channel_user_id`` so the portal still renders a stable
    identifier.

    ``last_intent`` reflects the most recent message's intent and helps
    the operator spot conversations that ended in handoff or booking.

    ``message_count`` is a per-conversation aggregate; the index on
    ``messages(tenant_id, conversation_id, created_at)`` makes the
    COUNT(*) acceptable at portal page sizes (≤ 50 rows per page).
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    channel: ConversationChannel
    channel_user_id: str
    customer_display_name: str
    last_message_at: str | None
    last_intent: MessageIntent | None
    message_count: int = Field(ge=0)
    chatwoot_conversation_id: int | None
    created_at: str


class ConversationListResponse(BaseModel):
    """Cursor-paginated list. Mirrors the notification feed shape so the
    portal table primitive can be reused without divergence.

    ``next_cursor`` is opaque ``<rfc3339>|<uuid>`` — clients pass it back
    verbatim. NULL when the page is the last one.
    """

    model_config = _FORBID

    items: list[ConversationListItem]
    next_cursor: str | None = None


class MessageDetail(BaseModel):
    """One message in the read-only detail pane.

    The shape carries the bot-side telemetry the operator wants to see —
    intent, which model the dispatcher picked, response latency, how
    many RAG chunks fed the reply — but **not** the chunk contents
    themselves (RAG bodies live in ``knowledge_chunks`` and surfacing
    them would bloat the payload for no portal-side use case).

    Tokens are optional everywhere because pre-Phase-4 messages don't
    carry telemetry and inbound user messages are never tokenized at
    write time (the loader heuristic kicks in only when a window is
    being assembled for the LLM).
    """

    model_config = _FORBID

    id: UUID
    role: MessageRole
    content: str
    intent: MessageIntent | None
    model_used: MessageModelUsed | None
    response_time_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    rag_chunk_count: int = Field(default=0, ge=0)
    # Who authored an assistant-role message — 'bot' (default) or 'human_agent'
    # for a human agent's Chatwoot reply mirrored into messages. Always 'bot'
    # for user/system rows. ``author_name`` carries the agent's display name for
    # a "replied by" label when available.
    author_type: MessageAuthorType = "bot"
    author_name: str | None = None
    created_at: str


class ConversationDetail(BaseModel):
    """Detail-pane response: the conversation header + its messages
    ordered oldest-first.

    Per Q-PHASE8-08 + portal_design_prompt.md §2, the detail surface is
    READ-ONLY. The "Reply in Chatwoot" CTA on the portal page is a
    cross-domain link to ``chat.getpolaris.my`` — no write surface here.
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    channel: ConversationChannel
    channel_user_id: str
    customer_display_name: str
    chatwoot_conversation_id: int | None
    last_message_at: str | None
    created_at: str
    messages: list[MessageDetail] = Field(default_factory=list)
