"""LLM-response Pydantic SSOT — Phase 4 PR-1.

Three shapes:

  * ``IntentTier`` — discrete output of Haiku 4.5 intent classifier
    per ADR-031. Drives model + max_tokens routing in
    ``services/llm.py::generate_response``.

  * ``UsageMetrics`` — token counts captured from the Anthropic SDK
    ``message.usage`` object. ``cache_read_tokens`` is the load-bearing
    field for prompt-cache hit-rate verification (Phase 4 exit criterion
    1) — non-zero on the second message proves caching is working.

  * ``ResponseEnvelope`` — output of ``services/llm.py::generate_response``.
    Persisted into the ``messages`` row by the Phase 4 pipeline
    orchestrator (PR-4) and rendered by the Phase 8 portal's
    conversation viewer.

The ``ModelUsed`` literal mirrors migration 028's
``messages.model_used`` CHECK constraint exactly. Drift between this
literal and the CHECK is caught by
``test_messages_persisted::test_model_used_matches_check_constraint``
(PR-4).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

IntentTier = Literal["simple", "medium", "hard", "handoff"]
# "handoff" is a synthetic tag (added 2026-05-19, audit L-1): the handoff
# reply text is a hardcoded template — no LLM call was made — so tagging
# it as ``haiku`` polluted the Phase 9 dashboard's per-model counters.
# Migration 040 relaxes the messages.model_used CHECK to allow this value.
ModelUsed = Literal["haiku", "sonnet", "gpt-4o-mini-fallback", "handoff"]
# ADR-031: EN, Bahasa Malaysia, Chinese-Simplified. Other languages are
# detected and answered in English with a one-line apology per the
# Sonnet system prompt; no separate model routing.
LanguageDetected = Literal["en", "ms", "zh-cn"]


class UsageMetrics(BaseModel):
    """Token counts from one Anthropic Messages API call.

    ``input_tokens`` and ``output_tokens`` map 1:1 to Anthropic's
    ``message.usage.input_tokens`` and ``message.usage.output_tokens``.

    ``cache_creation_tokens`` and ``cache_read_tokens`` come from the
    Anthropic prompt-cache extension (``usage.cache_creation_input_tokens``
    + ``usage.cache_read_input_tokens``). The OpenRouter fallback path
    leaves both at 0 (no prompt caching there per ADR-031 Amendment).
    """

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of input tokens served from cache.

        Used for Phase 4 exit criterion 1 verification (must be > 0 on
        the second message in a conversation). Returns 0.0 on a request
        with zero input tokens to avoid div-by-zero on degenerate
        responses.
        """
        if self.input_tokens <= 0:
            return 0.0
        return self.cache_read_tokens / self.input_tokens


class OutboundImage(BaseModel):
    """One product image to attach to a reply (Phase 15 PR-6).

    ``storage_path`` is the object key in the private ``polaris-product-images``
    bucket; the dispatch layer mints a short-TTL signed read URL from it at send
    time (so the URL is always fresh when Meta fetches it). ``caption`` is only
    honoured on WhatsApp (native image caption); IG/Messenger ignore it and the
    text rides as a separate message.
    """

    storage_path: str = Field(min_length=1, max_length=512)
    caption: str | None = Field(default=None, max_length=1024)
    # Agent-facing only: the product this image belongs to, used to annotate the
    # Chatwoot conversation ("N images sent for {product_name}") so a human agent
    # sees what the bot sent. Never shown to the customer. None when unknown.
    product_name: str | None = Field(default=None, max_length=255)


class ResponseEnvelope(BaseModel):
    """Output of ``services/llm.py::generate_response``.

    Carries everything the Phase 4 pipeline orchestrator needs to
    persist a ``messages`` row + dispatch the reply: the user-facing
    text, the intent tier the classifier picked, the actual model used
    (Haiku/Sonnet/OpenRouter fallback), language metadata for the
    handoff path, full token telemetry, and the IDs of the
    ``knowledge_chunks`` rows that fed the RAG context (so the portal
    can render the "sources" panel in Phase 8).

    ``fallback_used`` + ``fallback_reason`` are set when the Anthropic
    path failed and OpenRouter took over. Both are NULL on the happy
    path.
    """

    content: str
    intent: IntentTier
    model_used: ModelUsed
    language_detected: LanguageDetected
    usage: UsageMetrics
    latency_ms: int = Field(ge=0)
    rag_chunk_ids: list[str] = Field(default_factory=list)
    # Phase 15 PR-6: product images the bot chose to send alongside the text.
    # Empty on every non-product reply; the dispatch layer fans these out per
    # channel (WhatsApp caption-combine vs IG/Messenger text-then-image).
    images: list[OutboundImage] = Field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str | None = None
    # M-1 (audit 2026-05-19): the Sonnet stream → non-stream same-model
    # retry path was invisible to telemetry. ``fallback_used`` covers
    # provider-level fallback (Anthropic → OpenRouter); this covers the
    # transport-level fallback (stream → non-stream on the SAME model).
    # Both fields default False on the happy path.
    stream_recovered: bool = False
