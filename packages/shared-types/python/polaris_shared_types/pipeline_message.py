"""Pipeline-decision Pydantic SSOT — Phase 4 PR-4.

``PipelineDecision`` is the per-inbound-message projection of one
``pipeline.run()`` invocation. Returned by the orchestrator + rendered
by the Phase 8 portal's conversation viewer (with the ``ResponseEnvelope``
inside it driving the "intent / model / cache hit" badges).

Why this lives in shared-types (not in services/):
  - The Phase 8 portal renders it directly.
  - The admin ``/api/admin/pipeline/recent`` endpoint (deep-dive §6)
    serializes it for ops visibility.
  - Keeping it in shared-types means the TS shape stays in sync via
    the export.py pipeline; the portal team gets typed access without
    re-deriving from raw DB columns.

Phase 10 PR-1 additions:
  - ``assistant_message_id`` and ``response`` are now Optional because
    STOP-blocked and quota-exceeded early exits produce no assistant turn.
  - ``stop_blocked`` — True when the inbound text matched a STOP keyword
    and the contact was opted out.  No reply is dispatched.
  - ``quota_exceeded`` — True when the tenant's bot-replies cap for the
    current period was exhausted.  No reply is dispatched.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from polaris_shared_types.llm_response import ResponseEnvelope


class PipelineDecision(BaseModel):
    """Persisted projection of one ``pipeline.run()`` invocation.

    Fields:
      - ``conversation_id`` / ``tenant_id`` — scope
      - ``user_message_id`` — the messages.id row written for the
        inbound user turn
      - ``assistant_message_id`` — the messages.id row written for the
        bot's reply; ``None`` for stop_blocked / quota_exceeded early exits
        (no assistant turn is persisted in those cases).
      - ``response`` — full LLM envelope (intent, model, usage,
        language, RAG chunks). ``None`` for stop_blocked / quota_exceeded.
      - ``provider_message_id`` — the outbound wamid / mid returned by
        Meta when the reply was dispatched. ``None`` when dispatch was
        skipped (Phase 6 channel not yet wired) or failed (apology
        path) or an early exit occurred.
      - ``handoff_emitted`` — True when intent='handoff' triggered the
        handoff template instead of an LLM-generated reply. The portal
        renders this as a distinct conversation badge.
      - ``stop_blocked`` — Phase 10 PR-1: True when the inbound body
        matched a STOP keyword.  Contact is opted out; no reply sent.
      - ``quota_exceeded`` — Phase 10 PR-1: True when the tenant's
        monthly bot-replies cap was reached.  No reply sent.
      - ``handoff_suppressed`` — QA finding M1: True when the inbound
        was dropped from bot processing because the conversation is
        already human-handled (``conversations.handoff_at`` set, not
        yet cleared by a reverse-handoff). The customer's message is
        persisted and already visible to the agent in Chatwoot; the bot
        stays silent so it does not talk over the human.
    """

    conversation_id: UUID
    tenant_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID | None = None
    response: ResponseEnvelope | None = None
    provider_message_id: str | None = None
    handoff_emitted: bool = False
    stop_blocked: bool = False
    quota_exceeded: bool = False
    handoff_suppressed: bool = False
