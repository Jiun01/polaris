"""LLM wrapper — Phase 4 PR-1.

Single source of truth for every Anthropic / OpenRouter SDK call in
the Polaris API. Two public functions:

  * ``classify_intent(text)`` — Haiku 4.5 routes inbound messages to
    one of the four tiers per ADR-031. Output drives model + max_tokens
    routing in ``generate_response``. Garbage-output defaults to
    ``"medium"`` (safer than ``"simple"`` — would otherwise under-
    serve a hard question).

  * ``generate_response(...)`` — Haiku for simple/medium, Sonnet 4.6
    for hard. The system prompt block is the ONLY place
    ``cache_control={"type":"ephemeral", "ttl":...}`` appears in the
    Polaris codebase per deep-dive §9.1 + §7.4 finance-billing-ops
    audit gap A. RAG context is appended to the user message with NO
    cache_control. ``test_llm_cache_strategy.py`` is the load-bearing
    guard.

Cache strategy invariants (all enforced by tests):

  1. Per-block ``cache_control`` only on the system prompt
     ``TextBlockParam``.
  2. NO top-level ``cache_control`` kwarg on ``messages.create`` /
     ``messages.stream`` — that auto-applies the marker to the LAST
     cacheable block, which would be the RAG context (deep-dive §9.1).
  3. NO ``cache_control`` anywhere in user/assistant message content
     blocks.
  4. The string ``cache_control`` appears nowhere in the codebase
     outside this file and the ``test_llm_cache_strategy.py`` test.

Resilience: Anthropic 5xx / timeout / connection / rate-limit fall
back to OpenRouter ``openai/gpt-4o-mini`` UNCACHED per ADR-031
Amendment 2026-05-15. OpenAI auth/validation errors are NOT retried —
they mean code or config is wrong, not transient.

Streaming: Sonnet hard tier uses ``messages.stream(...)`` so the
Phase 4 pipeline orchestrator (PR-4) can fire Chatwoot typing-
indicator pings while the full reply completes. Haiku tiers stay
non-stream (already fast). On stream error, fall back to a single
non-stream call before escalating to OpenRouter — minor recoverable
issues (network hiccup mid-stream) shouldn't burn the whole reply.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, get_args
from uuid import UUID

import anthropic
import openai
from anthropic.types import ThinkingConfigParam
from polaris_shared_types import (
    IntentTier,
    LanguageDetected,
    ModelUsed,
    ResponseEnvelope,
    UsageMetrics,
)

from polaris_api import metrics
from polaris_api.config import get_settings
from polaris_api.services._llm_models import (
    LLMConfigError,
    LLMHardFailure,
    LLMTransientError,
)
from polaris_api.services._llm_prompts import (
    INTENT_CLASSIFIER_PROMPT,
    TenantPersona,
    build_email_draft_prompt,
    build_response_system_prompt,
)

if TYPE_CHECKING:
    from polaris_shared_types import KnowledgeChunkHit

logger = logging.getLogger(__name__)

_VALID_INTENTS: frozenset[str] = frozenset(get_args(IntentTier))
_FALLBACK_INTENT: IntentTier = "medium"


# ---------------------------------------------------------------------------
# Deterministic LLM stub (E2E only — gated by settings.llm_stub_enabled).
# Lets the full inbound pipeline run reproducibly + offline (no API key, no
# cost, no latency) so edge-case journeys are deterministic and CI-runnable.
# ---------------------------------------------------------------------------


def _stub_intent(text: str) -> IntentTier:
    """Keyword-driven deterministic intent for stub mode."""
    t = text.lower()
    if any(k in t for k in ("human", "agent", "complaint", "urgent", "refund")):
        return "handoff"
    if any(k in t for k in ("price", "cost", "how much", "book", "appointment", "quote")):
        return "hard"
    return "medium"


def _stub_envelope(user_message: str, intent: IntentTier, chunks: Any = None) -> ResponseEnvelope:
    """Canned deterministic reply for stub mode."""
    rag_chunk_ids = [str(c.id) for c in chunks] if chunks else []
    return ResponseEnvelope(
        content=(f"Thanks for reaching out! (stubbed E2E reply) You said: {user_message[:120]}"),
        intent=intent,
        model_used="haiku",
        language_detected="en",
        usage=UsageMetrics(
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        ),
        latency_ms=1,
        rag_chunk_ids=rag_chunk_ids,
    )


# Malay stopwords for heuristic language detection. Curated from
# Bahasa Malaysia frequency lists; 2+ hits in a message → ms. Kept
# small (< 30) so the linear scan is cheap on every inbound message.
# Phase 4 PR-6 demo telemetry will surface false-positive/-negative
# rates; revisit if either exceeds 5%.
_MALAY_STOPWORDS: frozenset[str] = frozenset(
    {
        # Formal / Bahasa Baku.
        "dan",
        "atau",
        "dengan",
        "untuk",
        "dalam",
        "pada",
        "ini",
        "itu",
        "yang",
        "adalah",
        "saya",
        "anda",
        "dia",
        "kita",
        "kami",
        "kamu",
        "kepada",
        "daripada",
        "sebagai",
        "juga",
        "akan",
        "tidak",
        "boleh",
        "mahu",
        "perlu",
        "jika",
        "kerana",
        "apabila",
        "selepas",
        "sebelum",
        "hingga",
        "semasa",
        "ya",
        "tak",
        "tetapi",
        "ialah",
        # Colloquial / Manglish / Bahasa Pasar — the dominant register on
        # WhatsApp/Telegram in MY. Their absence misclassified everyday Malay
        # (e.g. "korang ada buat sesi online tak?") as English. Kept distinct
        # enough from English tokens that the >=2 threshold avoids false hits.
        "ada",
        "nak",
        "korang",
        "buat",
        "macam",
        "camne",
        "kat",
        "dekat",
        "sini",
        "situ",
        "dah",
        "belum",
        "lagi",
        "nanti",
        "pun",
        "je",
        "jer",
        "ke",
        "ni",
        "tu",
        "jugak",
        "tahu",
        "tau",
        "guna",
        "pakai",
        "berapa",
        "bila",
        "mana",
        "kenapa",
        "macamana",
        "nya",
    }
)

# Anthropic SDK exception classes that map to "transient — try
# OpenRouter". 4xx auth / validation / not-found errors are NOT in
# this set — they mean code or config is broken, not a provider
# blip, and silently retrying via a different provider would mask
# the real bug.
_ANTHROPIC_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)

# Sonnet 5 defaults to adaptive thinking when ``thinking`` is omitted;
# the reply tiers were tuned with thinking OFF (see _generate_sonnet_streaming).
_THINKING_DISABLED: ThinkingConfigParam = {"type": "disabled"}

# OpenAI/OpenRouter equivalents for the second-tier fallback (very
# rare — if Anthropic AND OpenRouter both 5xx, we surface
# LLMHardFailure and the pipeline writes an apology reply).
_OPENAI_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)

_OPENROUTER_FALLBACK_MODEL = "openai/gpt-4o-mini"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# ----------------------------------------------------------------------
# Client factories — lazy, placeholder-key-guarded, test-monkeypatchable
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    """Lazy singleton Anthropic client.

    Raises ``LLMConfigError`` immediately if ``ANTHROPIC_API_KEY`` is
    unset or still the ``placeholder-anthropic-key`` sentinel from
    ``.env.example`` — failing fast at first call beats leaking a
    request to Anthropic with junk credentials. Tests use
    ``monkeypatch.setattr(llm, "_get_anthropic_client", ...)`` after
    ``_get_anthropic_client.cache_clear()``.
    """
    settings = get_settings()
    key = settings.anthropic_api_key
    if not key or key.startswith("placeholder-"):
        raise LLMConfigError(
            "ANTHROPIC_API_KEY is unset or still a placeholder. "
            "Set a real key in `.env` before any LLM call."
        )
    # Cap per-request latency (QA finding L6). A timeout raises
    # ``anthropic.APITimeoutError``, already in ``_ANTHROPIC_TRANSIENT_ERRORS``,
    # so the caller falls back to OpenRouter instead of blocking on a slow call.
    return anthropic.AsyncAnthropic(api_key=key, timeout=settings.llm_request_timeout_s)


@lru_cache(maxsize=1)
def _get_openrouter_client() -> openai.AsyncOpenAI:
    """Lazy singleton OpenAI client pointed at OpenRouter.

    OpenRouter exposes an OpenAI-compatible chat-completions API per
    ADR-031 Amendment 2026-05-15, so we reuse the official openai
    SDK with ``base_url`` swapped.
    """
    settings = get_settings()
    key = settings.openrouter_api_key
    if not key or key.startswith("placeholder-"):
        raise LLMConfigError(
            "OPENROUTER_API_KEY is unset or still a placeholder. "
            "Set a real key in `.env` before any LLM fallback call."
        )
    return openai.AsyncOpenAI(api_key=key, base_url=_OPENROUTER_BASE_URL)


# ----------------------------------------------------------------------
# Cache-strategy helpers — the load-bearing invariants live here
# ----------------------------------------------------------------------


def _build_cached_system_block(text: str) -> dict[str, Any]:
    """Build the ONE TextBlockParam that carries cache_control.

    This function is the only place ``cache_control`` is constructed
    in the Polaris codebase per deep-dive §9.1. The
    ``test_llm_cache_strategy::test_codebase_uses_cache_control_only_in_llm_py``
    test greps for the string outside this file and fails if it
    appears anywhere else.
    """
    settings = get_settings()
    return {
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral", "ttl": settings.llm_cache_ttl},
    }


def _extract_text(content_list: Any) -> str:
    """Pull text from the first TextBlock in ``response.content``.

    M-3 audit fix (2026-05-19): previously this returned ``""`` on a
    missing first-block ``.text`` attribute — silent empty, persisted
    as ``content=''`` in the messages row with no error trace. Now
    raises ``LLMHardFailure`` so the pipeline routes to its apology
    reply path instead of dispatching an empty WhatsApp message.

    The Anthropic SDK types ``content`` as a Union of TextBlock,
    ThinkingBlock, ToolUseBlock, etc. We never enable tools/thinking,
    so the first block is always TextBlock today. If the SDK reorders
    blocks (e.g. thinking-first in a future model), the fail-loud
    behaviour surfaces the regression instead of corrupting telemetry.

    Empty list → empty string (legitimate empty response, distinct
    from "blocks exist but no extractable text"). The caller's
    downstream content-empty handling still applies.
    """
    if not content_list:
        return ""
    # Walk the list to find the first block with a non-None ``.text``.
    # Doing one pass instead of just looking at index 0 means an
    # SDK reorder that puts thinking-first still finds the assistant
    # text block — at no extra cost since these lists are short.
    for block in content_list:
        text = getattr(block, "text", None)
        if text is not None:
            return str(text)
    raise LLMHardFailure(
        "Anthropic response contained blocks but no extractable text; "
        f"first block type={type(content_list[0]).__name__}"
    )


# Per-turn language nudge — written IN the target language so it both instructs
# AND primes the script. Appended as the LAST block of the trailing user turn:
# investigation (2026-06-15) showed the system-prompt directive alone loses to
# deep in-context history in another language (Haiku reverted 0/8 on 3 turns of
# Chinese history); a nudge at the end of the message stream is the most-recent
# context the model sees and reliably wins (Haiku + Sonnet → 8/8). Not stored as
# the customer's message — it only rides along on the API call.
_REPLY_LANGUAGE_NUDGE: dict[LanguageDetected, str] = {
    "en": "(Reply in English only.)",
    "ms": "(Balas dalam Bahasa Malaysia sahaja.)",
    "zh-cn": "(请只用简体中文回复。)",
}


def _language_nudge(language: LanguageDetected) -> str:
    return _REPLY_LANGUAGE_NUDGE.get(language, _REPLY_LANGUAGE_NUDGE["en"])


def _build_user_content_blocks(
    rag_context: str,
    user_message: str,
    language: LanguageDetected,
) -> list[dict[str, Any]]:
    """Build the content list for the trailing user message.

    RAG context (if any) comes FIRST as its own text block; the user's actual
    message follows; the per-turn language nudge comes LAST (most-recent context,
    the recency lever for mid-conversation language switches). None of the blocks
    carry cache_control: the RAG context changes every call (cache would never hit
    and would pollute Anthropic's cache slot), the user message is fresh content,
    and the nudge varies with the detected language.
    """
    blocks: list[dict[str, Any]] = []
    if rag_context:
        blocks.append({"type": "text", "text": rag_context})
    blocks.append({"type": "text", "text": user_message})
    blocks.append({"type": "text", "text": _language_nudge(language)})
    return blocks


# ----------------------------------------------------------------------
# Language detection — pure-Python heuristic per ADR-031
# ----------------------------------------------------------------------


def _detect_language(text: str) -> LanguageDetected:
    """Detect EN / Bahasa Malaysia / Simplified Chinese.

    Heuristic only — no LLM call. CJK Unicode range wins immediately
    (no realistic message mixes meaningful CJK with non-CJK content).
    For Latin-script text, 2+ Malay stopword hits → ms; else en.

    Other languages get classified as ``en`` and the Sonnet system
    prompt instructs the model to apologize-then-handoff if the
    inbound content is in an unsupported language. Phase 4 PR-6 demo
    telemetry will surface miss rates; if they exceed 5%, deep-dive
    §11 Q-PHASE4-02 promotes this to a Haiku-fallback classifier.
    """
    # CJK unified ideographs + extensions cover Simplified Chinese.
    # `unicodedata.name` is overkill for hot-path detection; check the
    # primary code-point block directly.
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            return "zh-cn"

    # Latin-script path. Normalize, lowercase, strip punctuation, count
    # stopword hits.
    normalized = unicodedata.normalize("NFKD", text).lower()
    tokens = [t.strip(".,!?;:\"'()[]{}/-") for t in normalized.split()]
    malay_hits = sum(1 for t in tokens if t in _MALAY_STOPWORDS)
    if malay_hits >= 2:
        return "ms"
    return "en"


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


async def classify_intent(text: str, *, tenant_id: UUID | None = None) -> IntentTier:
    """Classify an inbound user message into one of four intent tiers.

    Uses Haiku 4.5 with a small max_tokens budget (the classifier only
    needs to emit one word). On garbage output (a literal not in the
    set), defaults to ``"medium"`` — safer than ``"simple"`` because
    medium still gets Haiku but with a larger token budget, so a
    misclassified hard question at least gets a fuller answer attempt.

    ``tenant_id`` (optional) enables per-tenant cost telemetry: when
    provided, the classifier's Haiku token usage (incl. prompt-cache
    reads of the cached classifier prompt) is emitted under the
    ``intent_tier="classify"`` label so the admin cost dashboard accounts
    for classification overhead. It's cheap and cache-heavy, but real.
    Callers without a tenant context (benchmarks, tier-agnostic adapters)
    omit it and emit nothing.

    Raises ``LLMConfigError`` if ``ANTHROPIC_API_KEY`` is missing.
    Raises ``LLMTransientError`` on Anthropic 5xx / timeout — the
    pipeline orchestrator (PR-4) catches this and defaults to
    ``"medium"`` rather than failing the whole inbound message.
    """
    settings = get_settings()
    if settings.llm_stub_enabled:
        return _stub_intent(text)
    client = _get_anthropic_client()
    try:
        response = await client.messages.create(
            model=settings.llm_haiku_model,
            max_tokens=10,
            system=[_build_cached_system_block(INTENT_CLASSIFIER_PROMPT)],  # type: ignore[list-item]
            messages=[{"role": "user", "content": text}],
        )
    except _ANTHROPIC_TRANSIENT_ERRORS as exc:
        raise LLMTransientError(
            f"Anthropic classify transient error: {type(exc).__name__}"
        ) from exc

    if tenant_id is not None:
        _emit_haiku_usage(tenant_id, response.usage)

    raw = _extract_text(response.content).strip().lower()
    if raw in _VALID_INTENTS:
        return raw  # type: ignore[return-value]
    logger.warning(
        "classify_intent unparseable response (defaulting to %s): %r",
        _FALLBACK_INTENT,
        raw,
    )
    return _FALLBACK_INTENT


_CONDENSE_SYSTEM_PROMPT = (
    "You rewrite a customer's latest chat message into a SINGLE standalone "
    "search query for a knowledge base. Resolve references using the "
    "conversation (e.g. 'the KL one' -> 'KL outlet opening hours', 'is that "
    "cheaper' -> the thing being compared). Keep it short and keyword-focused, "
    "keep the customer's original language, and do NOT answer the question. "
    "Output ONLY the rewritten query text, nothing else."
)

# Cap how much history we feed the condenser — only the recent turns matter for
# resolving a referent, and a short prompt keeps this cheap (it runs per
# referential follow-up). Most-recent N messages.
_CONDENSE_HISTORY_TURNS = 6


async def condense_followup_query(
    conversation_history: list[dict[str, str]],
    current_message: str,
    *,
    tenant_id: UUID | None = None,
) -> str:
    """Rewrite a referential follow-up into a standalone search query — report F6.

    Retrieval embeds only the current message, so a bare follow-up like "what
    about the KL one?" can't resolve its referent and mis-retrieves. This
    produces a history-aware standalone query for the embedding + BM25 search
    (the LLM reply still receives the ORIGINAL message + full history).

    Fail-soft: returns ``current_message`` unchanged on stub mode, no history,
    any Anthropic error, or empty/over-long output — a condensation failure must
    never block or distort the reply. Cheap Haiku call; callers gate it to
    likely-referential messages to bound cost.
    """
    if not conversation_history or not current_message.strip():
        return current_message
    settings = get_settings()
    if settings.llm_stub_enabled:
        return current_message
    recent = conversation_history[-_CONDENSE_HISTORY_TURNS:]
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
    user = (
        f"Conversation so far:\n{transcript}\n\n"
        f"Latest customer message: {current_message}\n\n"
        f"Standalone search query:"
    )
    try:
        client = _get_anthropic_client()
        response = await client.messages.create(
            model=settings.llm_haiku_model,
            max_tokens=60,
            system=[_build_cached_system_block(_CONDENSE_SYSTEM_PROMPT)],  # type: ignore[list-item]
            messages=[{"role": "user", "content": user}],
        )
    except Exception:  # noqa: BLE001 — condensation is best-effort; fall back
        logger.warning("condense_followup_query failed — using original message")
        return current_message
    if tenant_id is not None:
        _emit_haiku_usage(tenant_id, response.usage)
    rewritten = _extract_text(response.content).strip().strip('"')
    # Guard against a runaway / empty rewrite — fall back to the original.
    if not rewritten or len(rewritten) > 300:
        return current_message
    return rewritten


# audit Batch B (B4): combines INTENT_CLASSIFIER_PROMPT's tier semantics with
# _CONDENSE_SYSTEM_PROMPT's rewrite rules into ONE system prompt, so a single
# forced-tool-use Haiku call returns both the tier AND a standalone search
# query for referential messages — replacing two sequential Haiku calls
# (classify_intent then condense_followup_query) with one.
_CLASSIFY_AND_CONDENSE_SYSTEM_PROMPT = """\
You are an intent classifier AND query-rewriter for a customer-service \
chatbot. Given the conversation so far and the customer's latest message, \
do TWO things and report both via the route_message tool:

1. Classify the latest message into exactly one of these four tiers:
  - simple: short FAQ, single fact lookup, greeting, price/location/hours \
question, basic product question. The bot can answer in under 100 tokens \
from a single knowledge chunk.
  - medium: multi-turn FAQ, comparison ("X vs Y"), product recommendation \
with one or two constraints, basic complaint triage. The bot needs to \
synthesize 2-3 knowledge chunks.
  - hard: complex multi-criteria recommendation, edge-case complaint \
requiring policy reasoning, ambiguous intent needing clarification turn, \
or any technical/procedural question that requires step-by-step \
explanation.
  - handoff: the user explicitly asks for a human agent, the message is \
hostile/abusive, or the topic is outside the bot's competence (legal \
advice, medical advice, financial decision-making, complaints about a \
specific staff member, refund/cancellation requiring approval).
  If unsure between two tiers, prefer the higher tier (medium > simple, \
hard > medium). If unsure whether to handoff, prefer handoff — a human is \
cheaper than a wrong bot reply on a sensitive topic.

2. Rewrite the latest message into a SINGLE standalone search query for a \
knowledge base, resolving references using the conversation (e.g. 'the KL \
one' -> 'KL outlet opening hours', 'is that cheaper' -> the thing being \
compared). Keep it short and keyword-focused, keep the customer's original \
language, and do NOT answer the question.

Always call route_message with both fields.\
"""


async def classify_and_condense(
    conversation_history: list[dict[str, str]],
    current_message: str,
    *,
    tenant_id: UUID | None = None,
) -> tuple[IntentTier, str]:
    """One Haiku call returning ``(tier, standalone_query)`` for a referential
    message — audit Batch B (B4).

    Replaces the sequential ``classify_intent`` + ``condense_followup_query``
    pair with a single forced-tool-use call for messages that both have
    history AND look referential (the caller gates this — see
    ``pipeline._looks_referential``). Uses the same tool-call extraction
    shape as ``generate_with_tools``.

    Fail-soft contract (mirrors both parent functions): on stub mode, empty
    history, ANY Anthropic error, missing/malformed tool output, an
    unparseable tier, or an empty/over-long query, falls back to the
    sequential ``classify_intent`` + original message — a condensation
    hiccup must never distort the reply, and this merged call must never be
    a SPOF for classification.
    """
    settings = get_settings()
    if settings.llm_stub_enabled:
        return (_stub_intent(current_message), current_message)
    if not conversation_history or not current_message.strip():
        intent = await classify_intent(current_message, tenant_id=tenant_id)
        return (intent, current_message)

    recent = conversation_history[-_CONDENSE_HISTORY_TURNS:]
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
    user = f"Conversation so far:\n{transcript}\n\nLatest customer message: {current_message}"
    tool_schema: dict[str, Any] = {
        "name": "route_message",
        "description": "Report the classified tier and standalone search query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["simple", "medium", "hard", "handoff"],
                },
                "query": {"type": "string"},
            },
            "required": ["tier", "query"],
        },
    }

    async def _fallback() -> tuple[IntentTier, str]:
        intent = await classify_intent(current_message, tenant_id=tenant_id)
        return (intent, current_message)

    # Mirrors generate_with_tools' dict[str, Any] kwargs pattern (llm.py) so
    # the forced tool_choice shape doesn't fight the SDK's create() overloads.
    kwargs: dict[str, Any] = {
        "model": settings.llm_haiku_model,
        "max_tokens": 200,
        "system": [_build_cached_system_block(_CLASSIFY_AND_CONDENSE_SYSTEM_PROMPT)],
        "messages": [{"role": "user", "content": user}],
        "tools": [tool_schema],
        "tool_choice": {"type": "tool", "name": "route_message"},
    }
    try:
        client = _get_anthropic_client()
        response = await client.messages.create(**kwargs)
    except Exception:  # noqa: BLE001 — merged call is best-effort; fall back
        logger.warning("classify_and_condense failed — falling back to sequential path")
        return await _fallback()

    if tenant_id is not None:
        _emit_haiku_usage(tenant_id, response.usage)

    tool_input: dict[str, Any] | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "route_message":
            tool_input = dict(block.input or {})
            break

    if tool_input is None:
        logger.warning("classify_and_condense: no route_message tool call — falling back")
        return await _fallback()

    raw_tier = tool_input.get("tier")
    raw_query = tool_input.get("query")
    if not isinstance(raw_tier, str) or raw_tier not in _VALID_INTENTS:
        logger.warning("classify_and_condense: invalid tier %r — falling back", raw_tier)
        return await _fallback()
    if not isinstance(raw_query, str):
        return await _fallback()

    query = raw_query.strip().strip('"')
    if not query or len(query) > 300:
        query = current_message

    return (raw_tier, query)  # type: ignore[return-value]


FOLLOWUP_SYSTEM_PROMPT = (
    "You write a SINGLE short follow-up message to re-engage a customer "
    "who went quiet mid-conversation. Rules: one message, at most ~3 short "
    "sentences; reply in the SAME language the customer used; warm and "
    "helpful, never pushy or salesy; lightly reference what they were "
    "interested in; offer to answer remaining questions or help finish. "
    "Never invent facts, prices, stock, or availability. Do not repeat "
    "earlier messages. No filler greetings like 'Dear customer'. Output "
    "ONLY the message text — no quotes, no preamble."
)


def _stub_followup(conversation_history: list[dict[str, str]]) -> str:
    """Deterministic stub for tests / llm_stub_enabled — no network."""
    _ = conversation_history
    return "Just checking in — are you still interested? Happy to help with anything."


async def generate_followup(
    *,
    conversation_history: list[dict[str, str]],
    persona: TenantPersona | None = None,
    max_tokens: int = 300,
) -> str:
    """Generate one re-engagement follow-up from a stalled conversation.

    Haiku 4.5 + a cached system block (cheap). The transcript rides as a
    single user turn — the conversation ended on the assistant, so we
    can't continue it directly; the model emits the nudge instead.
    Returns the message text.

    Raises ``LLMConfigError`` if the key is missing; ``LLMTransientError``
    on Anthropic 5xx/timeout (the follow-up worker catches + retries).

    No OpenRouter fallback by design — a follow-up is a non-critical,
    best-effort nudge; on a sustained Anthropic outage the worker exhausts
    its retry budget and drops the job rather than burning fallback spend
    on a message the customer didn't ask for.
    """
    settings = get_settings()
    if settings.llm_stub_enabled:
        return _stub_followup(conversation_history)

    system = FOLLOWUP_SYSTEM_PROMPT
    if persona is not None and (persona.assistant_name or persona.company_name):
        who = persona.assistant_name or "the assistant"
        company = f" at {persona.company_name}" if persona.company_name else ""
        system = f"You are {who}{company}. {FOLLOWUP_SYSTEM_PROMPT}"

    # Cap to the most recent turns so the function is safe to call from any
    # context (the worker already passes a load_history-bounded window).
    transcript = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in conversation_history[-20:]
    )
    user_content = (
        "Conversation so far:\n"
        f"{transcript}\n\n"
        "The customer stopped replying. Write one short follow-up to "
        "re-engage them per your instructions. Output only the message."
    )

    client = _get_anthropic_client()
    try:
        response = await client.messages.create(
            model=settings.llm_haiku_model,
            max_tokens=max_tokens,
            system=[_build_cached_system_block(system)],  # type: ignore[list-item]
            messages=[{"role": "user", "content": user_content}],
        )
    except _ANTHROPIC_TRANSIENT_ERRORS as exc:
        raise LLMTransientError(
            f"Anthropic followup transient error: {type(exc).__name__}"
        ) from exc

    return _extract_text(response.content).strip()


async def generate_response(
    *,
    tenant_id: UUID,
    rag_context: str,
    conversation_history: list[dict[str, str]],
    user_message: str,
    intent: IntentTier,
    chunks: list[KnowledgeChunkHit] | None = None,
    persona: TenantPersona | None = None,
    channel: str = "whatsapp",
    email_instructions: str | None = None,
) -> ResponseEnvelope:
    """Generate the assistant's reply.

    ``channel`` selects the formatting rules: chat channels (whatsapp /
    instagram / messenger / telegram / widget) get the plain-text chat prompt;
    ``channel="email"`` routes to the dedicated email-draft prompt (greeting /
    paragraphs / sign-off, drafting-for-an-agent framing). ``email_instructions``
    is the tenant's per-tenant email tone, applied only on the email path.

    Routes by intent tier:
      simple → Haiku, max_tokens=500, non-stream
      medium → Haiku, max_tokens=800, non-stream
      hard   → Sonnet, max_tokens=1000, STREAM
      handoff → not handled here; pipeline orchestrator should
                emit the handoff reply itself without calling
                ``generate_response`` (we raise ValueError as a
                fail-loud guard).

    ``conversation_history`` is a list of ``{"role", "content"}`` dicts
    in chronological order (oldest first). The trailing user message
    is NOT in this list — it's the ``user_message`` parameter.

    ``chunks`` (optional) feeds ``rag_chunk_ids`` into the
    ``ResponseEnvelope`` for the portal's sources panel; if ``None``
    the envelope ships an empty list.

    Raises ``ValueError`` if intent is ``"handoff"`` (caller bug).
    Raises ``LLMHardFailure`` if both Anthropic AND OpenRouter
    failed — the pipeline catches this and writes an apology reply.
    """
    if intent == "handoff":
        raise ValueError(
            "generate_response should not be called for intent='handoff'; "
            "pipeline.run emits the handoff template directly."
        )

    settings = get_settings()
    if settings.llm_stub_enabled:
        return _stub_envelope(user_message, intent, chunks)
    language = _detect_language(user_message)
    if channel == "email":
        system_prompt = build_email_draft_prompt(
            intent, language, persona, email_instructions=email_instructions
        )
    else:
        system_prompt = build_response_system_prompt(intent, language, persona, channel=channel)

    rag_chunk_ids = [str(c.id) for c in chunks] if chunks else []

    t0 = time.monotonic()
    try:
        if intent == "hard":
            envelope = await _generate_sonnet_streaming(
                settings=settings,
                system_prompt=system_prompt,
                rag_context=rag_context,
                conversation_history=conversation_history,
                user_message=user_message,
                language=language,
                intent=intent,
                rag_chunk_ids=rag_chunk_ids,
            )
        else:
            envelope = await _generate_haiku_nonstreaming(
                settings=settings,
                system_prompt=system_prompt,
                rag_context=rag_context,
                conversation_history=conversation_history,
                user_message=user_message,
                language=language,
                intent=intent,
                rag_chunk_ids=rag_chunk_ids,
            )
    except _ANTHROPIC_TRANSIENT_ERRORS as exc:
        logger.warning(
            "Anthropic transient (%s) — falling back to OpenRouter for tenant_id=%s",
            type(exc).__name__,
            tenant_id,
        )
        envelope = await _generate_openrouter_fallback(
            settings=settings,
            system_prompt=system_prompt,
            rag_context=rag_context,
            conversation_history=conversation_history,
            user_message=user_message,
            language=language,
            intent=intent,
            rag_chunk_ids=rag_chunk_ids,
            fallback_reason=f"anthropic_{type(exc).__name__}",
        )

    envelope.latency_ms = int((time.monotonic() - t0) * 1000)
    # Mechanically enforce the "no em dashes" format rule (the model ignores it
    # often enough to leak em dashes into chat replies — report finding F9).
    envelope.content = _strip_em_dashes(envelope.content)

    # Emit per-tenant token counters for the admin cost dashboard. Covers
    # Haiku/Sonnet and the OpenRouter fallback (disambiguated by model_used).
    # Cache write/read tokens are emitted separately so the cost layer can
    # price them at Anthropic's ~1.25x / ~0.1x prompt-cache rates.
    metrics.record_anthropic_usage(
        tenant_id=tenant_id,
        intent_tier=intent,
        model_used=envelope.model_used,
        input_tokens=envelope.usage.input_tokens,
        output_tokens=envelope.usage.output_tokens,
        cache_write_tokens=envelope.usage.cache_creation_tokens,
        cache_read_tokens=envelope.usage.cache_read_tokens,
    )
    return envelope


# ----------------------------------------------------------------------
# Internal: model-specific call paths
# ----------------------------------------------------------------------


def _strip_em_dashes(text: str) -> str:
    """Enforce the format rule "never use em dashes" mechanically.

    The chat + email prompts both forbid em dashes, but Haiku ignores the
    instruction often enough that we sanitise the final text. Replaces the em
    dash (U+2014) and horizontal bar (U+2015), with any surrounding spaces, by a
    comma + space (the prompt's suggested substitution). The en dash (U+2013) is
    left alone so numeric ranges like ``RM50–RM100`` survive.
    """
    if not text:
        return text
    return re.sub(r"\s*[—―]\s*", ", ", text)


def _extract_json_object(text: str) -> str:
    """Slice the first ``{...}`` object out of an LLM reply (tolerates code
    fences / prose around it). Returns ``"{}"`` when none is found."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return "{}"
    return text[start : end + 1]


async def select_product_skus(
    user_message: str,
    candidates: list[dict[str, str]],
) -> list[str]:
    """Structured decision: which candidate product ids (if any) to show the
    customer as photos (Phase 15 PR-6).

    ``candidates`` is ``[{"id", "name", "description"}]``. Returns the subset of
    ids whose PHOTO the customer wants to see right now — empty when they are
    not asking to view a product (e.g. a yes/no question) or on ANY failure
    (fail-safe: a selection error must never block the text reply). Uses the
    Haiku tier; the model returns a JSON object ``{"skus": [...]}``.
    """
    if not candidates:
        return []
    valid_ids = {c["id"] for c in candidates}
    catalog = "\n".join(
        f"- id={c['id']}: {c['name']} — {c['description'][:200]}" for c in candidates
    )
    system = (
        "You decide which product PHOTOS to send a customer in a chat reply. "
        'Return ONLY a JSON object of the form {"skus": ["<id>", ...]} listing '
        "the ids of products whose photo the customer wants to SEE right now. "
        "Return an empty list if they are not asking to view a product (e.g. a "
        "yes/no or policy question). Use only ids from the list."
    )
    user = f"Customer message: {user_message}\n\nProducts:\n{catalog}"
    try:
        settings = get_settings()
        model, _, _ = _model_for_intent(settings, "simple")
        client = _get_anthropic_client()
        # `system` is a short (<1KB) static instruction and is intentionally
        # NOT wrapped in _build_cached_system_block: it falls below Anthropic's
        # minimum cacheable block size, so a cache_control marker would be a
        # no-op here. (cf. the OpenRouter fallback, documented as UNCACHED.)
        message = await client.messages.create(
            model=model,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = _extract_text(message.content).strip()
        parsed = json.loads(_extract_json_object(text))
        raw_ids = parsed.get("skus", []) if isinstance(parsed, dict) else []
        # Preserve model order, dedup, keep only ids we offered.
        seen: set[str] = set()
        chosen: list[str] = []
        for i in raw_ids:
            sid = str(i)
            if sid in valid_ids and sid not in seen:
                seen.add(sid)
                chosen.append(sid)
        return chosen
    except Exception:  # noqa: BLE001 — fail-safe: never block the reply on selection
        logger.warning("select_product_skus failed — sending reply without product images")
        return []


def _max_tokens_for_intent(settings: Any, intent: IntentTier) -> int:
    """Per-tier max_tokens cap per ADR-031 Amendment 2026-05-15."""
    if intent == "hard":
        return int(settings.llm_max_tokens_hard)
    if intent == "medium":
        return int(settings.llm_max_tokens_medium)
    return int(settings.llm_max_tokens_simple)


def _model_for_intent(
    settings: Any, intent: IntentTier
) -> tuple[str, ModelUsed, ThinkingConfigParam | None]:
    """Returns (model_id_for_sdk, model_used_for_telemetry, thinking_param).

    ``thinking_param`` is ``_THINKING_DISABLED`` for the hard (Sonnet
    streaming) tier — see ``_generate_sonnet_streaming`` for why adaptive
    thinking must be pinned off — and ``None`` for every other tier, which
    never passes a ``thinking=`` kwarg at all.
    """
    if intent == "hard":
        return str(settings.llm_sonnet_model), "sonnet", _THINKING_DISABLED
    return str(settings.llm_haiku_model), "haiku", None


def _build_anthropic_messages(
    rag_context: str,
    conversation_history: list[dict[str, str]],
    user_message: str,
    language: LanguageDetected,
) -> list[dict[str, Any]]:
    """Build the ``messages`` parameter for Anthropic SDK."""
    msgs: list[dict[str, Any]] = list(conversation_history)
    msgs.append(
        {
            "role": "user",
            "content": _build_user_content_blocks(rag_context, user_message, language),
        }
    )
    return msgs


async def _generate_haiku_nonstreaming(
    *,
    settings: Any,
    system_prompt: str,
    rag_context: str,
    conversation_history: list[dict[str, str]],
    user_message: str,
    language: LanguageDetected,
    intent: IntentTier,
    rag_chunk_ids: list[str],
) -> ResponseEnvelope:
    client = _get_anthropic_client()
    model, model_used, _ = _model_for_intent(settings, intent)
    response = await client.messages.create(
        model=model,
        max_tokens=_max_tokens_for_intent(settings, intent),
        system=[_build_cached_system_block(system_prompt)],  # type: ignore[list-item]
        messages=_build_anthropic_messages(  # type: ignore[arg-type]
            rag_context,
            conversation_history,
            user_message,
            language,
        ),
    )
    content = _extract_text(response.content)
    return ResponseEnvelope(
        content=content,
        intent=intent,
        model_used=model_used,
        language_detected=language,
        usage=_usage_from_anthropic(response.usage),
        latency_ms=0,  # filled by caller
        rag_chunk_ids=rag_chunk_ids,
    )


async def _generate_sonnet_streaming(
    *,
    settings: Any,
    system_prompt: str,
    rag_context: str,
    conversation_history: list[dict[str, str]],
    user_message: str,
    language: LanguageDetected,
    intent: IntentTier,
    rag_chunk_ids: list[str],
) -> ResponseEnvelope:
    """Sonnet hard tier — streamed.

    PR-4's pipeline orchestrator wraps this with the Chatwoot typing-
    indicator side-channel: every ``stream.text_stream`` event fires
    a typing ping. PR-1 just consumes the stream and surfaces the
    final message; the side-channel hook is added (without changing
    this signature) in PR-4.

    If the stream itself errors mid-flight, fall back to a single
    non-stream Sonnet call before escalating to OpenRouter. A network
    hiccup mid-stream shouldn't waste the whole reply.
    """
    client = _get_anthropic_client()
    model, model_used, thinking = _model_for_intent(settings, intent)
    max_tokens = _max_tokens_for_intent(settings, intent)
    # Only the hard tier dispatches here (generate_response), so the returned
    # thinking param is always _THINKING_DISABLED — narrow for the SDK type.
    assert thinking is not None  # noqa: S101

    try:
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            # Sonnet 5 turns adaptive thinking ON by default when ``thinking``
            # is omitted (Sonnet 4.6 ran it OFF). Thinking tokens would eat the
            # tight hard-tier ``max_tokens`` budget and truncate the reply, plus
            # add an empty-thinking pause before text streams. Pin it OFF to
            # preserve the 4.6 behaviour this tier was tuned for.
            thinking=thinking,
            system=[_build_cached_system_block(system_prompt)],  # type: ignore[list-item]
            messages=_build_anthropic_messages(  # type: ignore[arg-type]
                rag_context,
                conversation_history,
                user_message,
                language,
            ),
        ) as stream:
            # Consume the stream — PR-4 will tee these events into a
            # Chatwoot typing-indicator callback. PR-1 just drains.
            async for _ in stream.text_stream:
                pass
            final = await stream.get_final_message()
    except _ANTHROPIC_TRANSIENT_ERRORS:
        # Don't re-raise — fall back to a single non-stream Sonnet
        # call. If THAT also fails with a transient error, it
        # bubbles to ``generate_response``'s OpenRouter handler.
        logger.warning(
            "Sonnet stream errored; retrying once non-streaming for model=%s",
            model,
        )
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            thinking=thinking,  # see stream call above — pin OFF on Sonnet 5
            system=[_build_cached_system_block(system_prompt)],  # type: ignore[list-item]
            messages=_build_anthropic_messages(  # type: ignore[arg-type]
                rag_context,
                conversation_history,
                user_message,
                language,
            ),
        )
        content = _extract_text(response.content)
        return ResponseEnvelope(
            content=content,
            intent=intent,
            model_used=model_used,
            language_detected=language,
            usage=_usage_from_anthropic(response.usage),
            latency_ms=0,
            rag_chunk_ids=rag_chunk_ids,
            # M-1 audit fix (2026-05-19): mark the silent same-model
            # transport fallback so the Phase 9 dashboard can chart
            # stream-recovery rate. We DIDN'T fall back to OpenRouter,
            # so ``fallback_used`` stays False.
            stream_recovered=True,
        )

    content = _extract_text(final.content)
    return ResponseEnvelope(
        content=content,
        intent=intent,
        model_used=model_used,
        language_detected=language,
        usage=_usage_from_anthropic(final.usage),
        latency_ms=0,
        rag_chunk_ids=rag_chunk_ids,
    )


async def _generate_openrouter_fallback(
    *,
    settings: Any,
    system_prompt: str,
    rag_context: str,
    conversation_history: list[dict[str, str]],
    user_message: str,
    language: LanguageDetected,
    intent: IntentTier,
    rag_chunk_ids: list[str],
    fallback_reason: str,
) -> ResponseEnvelope:
    """OpenRouter ``openai/gpt-4o-mini`` UNCACHED fallback.

    Per ADR-031 Amendment 2026-05-15: no prompt caching here. The
    OpenAI-style messages list inlines the RAG context into the
    system message, then carries the conversation as standard
    ``role`` / ``content`` strings. ``max_tokens`` matches the
    intended Anthropic-side cap so the user gets a reply of roughly
    the same length.

    Failure here escalates to ``LLMHardFailure`` — the pipeline
    orchestrator (PR-4) catches it and writes an apology reply.
    """
    client = _get_openrouter_client()

    # Per code-review L-3: keep the RAG-context delimiter consistent
    # with what ``_RAG_INJECTION_GUARD`` references (``<knowledge_chunk>``
    # tags). The fallback inlines RAG content into the system message
    # — without the matching wrapper, a crafted chunk could inject in
    # this path while the guard's instruction (which talks about
    # <knowledge_chunk> specifically) wouldn't apply. ``rag_context``
    # is already pre-formatted by ``services/_llm_prompts.format_rag_context``
    # with the wrapper tags, so we just inline it as-is.
    or_system = system_prompt
    if rag_context:
        or_system = f"{system_prompt}\n\n{rag_context}"

    or_messages: list[dict[str, str]] = [
        {"role": "system", "content": or_system},
        *conversation_history,
        # Per-turn language nudge rides on the final user turn (same recency lever
        # as the Anthropic path) so the fallback honors a mid-conversation switch.
        {"role": "user", "content": f"{user_message}\n\n{_language_nudge(language)}"},
    ]

    try:
        response = await client.chat.completions.create(
            model=_OPENROUTER_FALLBACK_MODEL,
            messages=or_messages,  # type: ignore[arg-type]
            max_tokens=_max_tokens_for_intent(settings, intent),
        )
    except _OPENAI_TRANSIENT_ERRORS as exc:
        raise LLMHardFailure(f"Both Anthropic and OpenRouter failed: {type(exc).__name__}") from exc

    choice = response.choices[0] if response.choices else None
    content = choice.message.content or "" if choice else ""
    usage = response.usage
    return ResponseEnvelope(
        content=content,
        intent=intent,
        model_used="gpt-4o-mini-fallback",
        language_detected=language,
        usage=UsageMetrics(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cache_creation_tokens=0,  # OpenRouter is uncached per ADR-031 Amendment
            cache_read_tokens=0,
        ),
        latency_ms=0,
        rag_chunk_ids=rag_chunk_ids,
        fallback_used=True,
        fallback_reason=fallback_reason,
    )


# ----------------------------------------------------------------------
# Usage parsing — defensive against SDK shape drift
# ----------------------------------------------------------------------


def _usage_from_anthropic(usage: Any) -> UsageMetrics:
    """Extract token counts from Anthropic ``Message.usage``.

    Defensive against SDK additions: uses ``getattr`` with defaults so
    a new optional field doesn't break us. The two cache fields are
    the load-bearing ones for Phase 4 exit criterion 1 (cache hit
    rate verifiable).
    """
    return UsageMetrics(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_creation_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )


def _emit_haiku_usage(tenant_id: UUID, usage: Any) -> None:
    """Emit per-tenant token counters for a Haiku helper call (classify /
    condense). Both share the ``intent_tier="classify"`` / ``model_used="haiku"``
    cost bucket. ``usage`` is the raw Anthropic ``Message.usage``.
    """
    metrics_usage = _usage_from_anthropic(usage)
    metrics.record_anthropic_usage(
        tenant_id=tenant_id,
        intent_tier="classify",
        model_used="haiku",
        input_tokens=metrics_usage.input_tokens,
        output_tokens=metrics_usage.output_tokens,
        cache_write_tokens=metrics_usage.cache_creation_tokens,
        cache_read_tokens=metrics_usage.cache_read_tokens,
    )


# ----------------------------------------------------------------------
# Test helpers (kept here so tests don't depend on private SDK shapes)
# ----------------------------------------------------------------------


def _reset_clients_for_testing() -> None:
    """Clear cached client singletons. Used by tests before monkeypatch.

    Defensive against repeated calls within one test: a fixture may
    have already swapped the factory for a plain lambda (no
    ``cache_clear`` attr). ``getattr`` guard keeps test setup order
    flexible — ``mock_openrouter`` can compose with ``mock_anthropic``
    in either order without exploding.
    """
    for fn in (_get_anthropic_client, _get_openrouter_client):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()


# ----------------------------------------------------------------------
# Agent tool-use (ADR-063) — the first messages.create(tools=...) call.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation the model requested."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolTurn:
    """Normalized one-turn result of a tool-use exchange.

    Decouples the agent loop from the Anthropic SDK types so the loop is trivial
    to stub/test. ``raw_assistant_content`` is the content list to append back as
    the assistant turn before sending ``tool_result``s.
    """

    stop_reason: str
    text: str
    tool_calls: tuple[ToolCall, ...]
    raw_assistant_content: Any
    usage: UsageMetrics


def _stub_tool_turn() -> ToolTurn:
    text = "[stubbed agent reply]"
    return ToolTurn(
        stop_reason="end_turn",
        text=text,
        tool_calls=(),
        raw_assistant_content=[{"type": "text", "text": text}],
        usage=UsageMetrics(input_tokens=0, output_tokens=0),
    )


async def generate_with_tools(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 2048,
    model_tier: IntentTier = "simple",
) -> ToolTurn:
    """One turn of the agent tool-use loop.

    Sends the conversation + the offered ``tools`` to Claude and returns a
    normalized ``ToolTurn``. Defaults to the Haiku tier (``model_tier="simple"``)
    — cost-efficient for tool selection + replies; pass ``"hard"`` to escalate a
    turn to Sonnet. When ``tools`` is empty, no ``tools``/``tool_choice`` is sent
    so the model just replies. ``max_tokens`` is generous (2048) so a tool-
    informed reply isn't truncated. Honors ``llm_stub_enabled`` so E2E/CI never
    hit Anthropic.

    Resilience: mirrors ``generate_response`` — Anthropic 5xx / timeout /
    connection / rate-limit falls back to OpenRouter ``openai/gpt-4o-mini``
    (tools NOT offered on the fallback call; see module note). Both providers
    failing raises ``LLMHardFailure``; the agent pipeline integration layer
    catches this and falls through to the flow/classic reply path instead of
    dropping the message silently.
    """
    settings = get_settings()
    if settings.llm_stub_enabled:
        return _stub_tool_turn()

    client = _get_anthropic_client()
    model, _, thinking = _model_for_intent(settings, model_tier)  # Haiku by default
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [_build_cached_system_block(system_prompt)],
        "messages": messages,
    }
    if thinking is not None:
        # Hard tier → Sonnet 5, whose adaptive thinking defaults ON and would
        # eat the tool-loop max_tokens budget — same pin as the reply tiers.
        kwargs["thinking"] = thinking
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = {"type": "auto"}

    try:
        response = await client.messages.create(**kwargs)
    except _ANTHROPIC_TRANSIENT_ERRORS as exc:
        logger.warning(
            "Anthropic transient (%s) in generate_with_tools — falling back "
            "to OpenRouter (no tools on fallback turn)",
            type(exc).__name__,
        )
        return await _generate_tools_openrouter_fallback(
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
            fallback_reason=f"anthropic_{type(exc).__name__}",
        )

    tool_calls = tuple(
        ToolCall(id=block.id, name=block.name, input=dict(block.input or {}))
        for block in response.content
        if getattr(block, "type", None) == "tool_use"
    )
    return ToolTurn(
        stop_reason=response.stop_reason or "end_turn",
        text=_extract_text(response.content),
        tool_calls=tool_calls,
        raw_assistant_content=response.content,
        usage=_usage_from_anthropic(response.usage),
    )


async def _generate_tools_openrouter_fallback(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    fallback_reason: str,
) -> ToolTurn:
    """OpenRouter fallback for a tool-use turn — Anthropic-side outage only.

    Tools are deliberately NOT offered to OpenRouter: gpt-4o-mini's tool-call
    JSON shape differs from Anthropic's, and ``ToolTurn.raw_assistant_content``
    must round-trip back into a NEXT Anthropic call's message history (the
    loop alternates assistant/tool_result turns) — mixing shapes there would
    corrupt the next turn. Instead the fallback asks for a plain best-effort
    text reply with no tool access; ``run_agent_reply`` still gets `stop_reason
    != "tool_use"` and ends the loop with this text, same as hitting the
    iteration cap. Failure here raises ``LLMHardFailure``.
    """
    client = _get_openrouter_client()
    or_messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        content = m.get("content")
        # Tool-result / tool-use content blocks don't have an OpenAI-compatible
        # shape; degrade non-string content to a short marker rather than
        # crashing the fallback on a shape mismatch.
        or_messages.append(
            {
                "role": m.get("role", "user"),
                "content": content if isinstance(content, str) else "[tool exchange omitted]",
            }
        )

    try:
        response = await client.chat.completions.create(
            model=_OPENROUTER_FALLBACK_MODEL,
            messages=or_messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
        )
    except _OPENAI_TRANSIENT_ERRORS as exc:
        raise LLMHardFailure(
            f"Both Anthropic and OpenRouter failed in generate_with_tools: {type(exc).__name__}"
        ) from exc

    choice = response.choices[0] if response.choices else None
    text = (choice.message.content or "") if choice else ""
    usage = response.usage
    return ToolTurn(
        stop_reason="end_turn",
        text=text,
        tool_calls=(),
        raw_assistant_content=[{"type": "text", "text": text}],
        usage=UsageMetrics(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        ),
    )
