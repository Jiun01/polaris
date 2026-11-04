"""Conversation Pydantic SSOT — Phase 4 PR-3.

Two shapes:

  * ``ConversationMessage`` — one turn in a conversation. Matches the
    Anthropic SDK ``{"role", "content"}`` message shape so the loader's
    output threads straight into ``services/llm.py::generate_response``
    without conversion. ``token_count`` is optional because Phase 2
    inbound messages aren't tokenized at write-time (we don't burn a
    Haiku call to count tokens we'll re-pay for at LLM time);
    ``load_history`` falls back to a ``len(content) // 4`` heuristic
    when NULL.

  * ``ConversationWindow`` — the trimmed slice returned by
    ``load_history``. Carries the messages + a ``truncated_by`` flag
    (so observability can chart "how often does the 8k token cap kick
    in vs the 20-message cap?") + the total token count for the
    Phase 9 dashboard.

Window-trim policy per ADR-031: keep at most ``max_messages`` turns OR
``max_tokens`` total tokens, whichever ceiling hits first walking
from the most-recent message backward.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ConversationRole = Literal["user", "assistant", "system"]
TruncatedBy = Literal["message_count", "token_count", "none"]


class ConversationMessage(BaseModel):
    """One turn in a conversation, ordered chronologically (oldest first)
    when in a window.

    ``role`` mirrors Anthropic SDK's role enum so the loader output
    threads straight into the SDK's ``messages`` parameter — no
    intermediate conversion.

    ``token_count`` is the historical token count if known. NULL when
    the message was written from an inbound channel (no Haiku
    pre-count); the loader heuristic uses ``len(content) // 4`` for
    NULL rows when computing the window cap.
    """

    role: ConversationRole
    content: str
    created_at: datetime
    token_count: int | None = None


class ConversationWindow(BaseModel):
    """Window of recent messages for an LLM call.

    Returned from ``services/conversation.py::load_history``. Messages
    are chronological (oldest first) — ready to thread into the
    Anthropic SDK ``messages`` parameter directly.

    ``truncated_by`` lets the Phase 9 dashboard chart how often each
    ceiling triggers — drives the data feeding any future tuning of
    the 20-msg / 8k-token defaults.
    """

    conversation_id: UUID
    tenant_id: UUID
    messages: list[ConversationMessage] = Field(default_factory=list)
    truncated_by: TruncatedBy = "none"
    total_tokens: int = Field(default=0, ge=0)
