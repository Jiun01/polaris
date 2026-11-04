"""Regression tests for the 2026-05-19 agent-architecture audit fixes.

Each test pins one of the eight findings from
``docs/Polaris V2/_audits/2026-05-19_phase4_5_agent_architecture_audit.md``
so a future change that undoes the fix surfaces a focused failure
rather than a vague telemetry drift.

The H-1 and H-2 fixes were originally guarded inside the flow-runtime suite
(``apps/api/tests/flow_runtime/test_node_llm_reply.py``, since retired —
audit Batch D) because they touched port wiring; that coverage moved with
the ports/adapters relocation to ``services/agent/ports.py``. This file
covers the Phase 4 / shared-types side:

  * L-1 — ``ModelUsed`` literal includes ``"handoff"``.
  * M-1 — ``ResponseEnvelope.stream_recovered`` field exists + defaults
    False on the happy path, flips True in the Sonnet stream → non-stream
    same-model fallback.
  * M-2 — ``reply_dispatch`` clips bodies above 4096 chars with a
    truncation marker before calling ``meta_client.send_text``.
  * M-3 — ``services.llm._extract_text`` raises LLMHardFailure when the
    SDK returns blocks with no extractable text (silent-empty
    eliminated).
  * L-2 — ``conversation.load_history`` filters out the
    ``[media-only message]`` placeholder so it doesn't pollute the
    LLM context window.
  * H-3 — ``build_response_system_prompt`` honours a tenant-supplied
    ``TenantPersona`` so multi-tenant cutover doesn't leak "Pol /
    Polaris" branding.
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from polaris_api.services._llm_models import LLMHardFailure
from polaris_api.services._llm_prompts import (
    TenantPersona,
    build_response_system_prompt,
)
from polaris_api.services.llm import _extract_text
from polaris_api.services.reply_dispatch import (
    ConversationRef,
    _clip_for_whatsapp,
    send_reply,
)
from polaris_shared_types import ModelUsed, ResponseEnvelope, UsageMetrics

# ----------------------------------------------------------------------
# L-1 — ModelUsed includes "handoff"
# ----------------------------------------------------------------------


def test_l1_model_used_literal_includes_handoff() -> None:
    """Pre-audit: handoff replies were tagged ``model_used='haiku'``
    in telemetry — every handoff (a hardcoded template — NO LLM
    call) showed up in Haiku per-model counters. Migration 040
    relaxed the CHECK; the Literal now includes the dedicated tag.
    """
    values = set(get_args(ModelUsed))
    assert "handoff" in values, (
        "ModelUsed Literal must include 'handoff' (L-1 audit fix). "
        "If you intentionally removed it, also update messages.model_used "
        "CHECK in packages/db-migrations/."
    )
    # Other expected members survive — no accidental shrinkage.
    assert {"haiku", "sonnet", "gpt-4o-mini-fallback"} <= values


# ----------------------------------------------------------------------
# M-1 — stream_recovered field on ResponseEnvelope
# ----------------------------------------------------------------------


def test_m1_response_envelope_has_stream_recovered_default_false() -> None:
    """The new field must default to False so existing call sites that
    don't pass it (every Haiku call, every non-fallback Sonnet call)
    keep persisting ``stream_recovered=False``."""
    envelope = ResponseEnvelope(
        content="hi",
        intent="simple",
        model_used="haiku",
        language_detected="en",
        usage=UsageMetrics(input_tokens=1, output_tokens=1),
        latency_ms=10,
    )
    assert envelope.stream_recovered is False


def test_m1_response_envelope_can_set_stream_recovered_true() -> None:
    """Sonnet stream → non-stream same-model fallback flips the flag."""
    envelope = ResponseEnvelope(
        content="hi",
        intent="hard",
        model_used="sonnet",
        language_detected="en",
        usage=UsageMetrics(input_tokens=10, output_tokens=20),
        latency_ms=100,
        stream_recovered=True,
    )
    assert envelope.stream_recovered is True
    # ``fallback_used`` stays False — stream recovery is NOT a
    # provider fallback (we stayed on Sonnet).
    assert envelope.fallback_used is False


# ----------------------------------------------------------------------
# M-2 — 4096-char clip in reply_dispatch
# ----------------------------------------------------------------------


def test_m2_short_body_passes_through_untruncated() -> None:
    body = "Hello, how can I help?"
    clipped, truncated = _clip_for_whatsapp(body)
    assert clipped == body
    assert truncated is False


def test_m2_oversize_body_clipped_with_marker() -> None:
    """WhatsApp Cloud API rejects bodies > 4096 chars with HTTP 400.
    The clip keeps the head + appends a marker so the user sees a
    truncated answer rather than silence."""
    body = "x" * 5000
    clipped, truncated = _clip_for_whatsapp(body)
    assert truncated is True
    assert len(clipped) <= 4096
    assert clipped.endswith("[truncated]")


def test_m2_boundary_at_exactly_4096_chars_not_truncated() -> None:
    """The cap is inclusive — 4096 chars is the legal max, not the
    first illegal value."""
    body = "x" * 4096
    clipped, truncated = _clip_for_whatsapp(body)
    assert truncated is False
    assert clipped == body


@pytest.mark.asyncio
async def test_m2_send_reply_passes_clipped_body_to_meta_client() -> None:
    """End-to-end: reply_dispatch.send_reply must call the meta client
    with the clipped body, not the raw response.content. Failure here
    means a Sonnet over-budget reply will 400 from Meta."""
    long_content = "y" * 4500
    envelope = ResponseEnvelope(
        content=long_content,
        intent="hard",
        model_used="sonnet",
        language_detected="en",
        usage=UsageMetrics(input_tokens=1, output_tokens=1),
        latency_ms=10,
    )
    meta_client = MagicMock()
    meta_client.send_text = AsyncMock(return_value="wamid.fake")
    # whatsapp send with explicit per-channel creds exercises the _send_whatsapp
    # clip path (ADR-066 removed the demo-channel/demo-number fallback).
    await send_reply(
        conversation=ConversationRef(channel="whatsapp", channel_user_id="60123"),
        response=envelope,
        meta_client=meta_client,
        whatsapp_phone_number_id="1234567890",
        whatsapp_access_token="EAA-tenant-token",
    )
    assert meta_client.send_text.await_count == 1
    sent_body = meta_client.send_text.await_args.kwargs["body"]
    assert len(sent_body) <= 4096
    assert sent_body.endswith("[truncated]")


# ----------------------------------------------------------------------
# M-3 — _extract_text fails loud on missing TextBlock
# ----------------------------------------------------------------------


def test_m3_extract_text_returns_string_from_first_text_block() -> None:
    """Happy path: first block has .text — return it."""

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    assert _extract_text([_Block("hello")]) == "hello"


def test_m3_extract_text_walks_past_non_text_blocks() -> None:
    """Defensive: a future SDK that emits thinking-first should still
    surface the assistant text from a later block instead of returning
    empty."""

    class _ThinkingBlock:
        pass  # no .text attribute

    class _TextBlock:
        text = "the real reply"

    assert _extract_text([_ThinkingBlock(), _TextBlock()]) == "the real reply"


def test_m3_extract_text_empty_list_returns_empty_string() -> None:
    """Legitimate empty response (no blocks at all) ≠ corruption."""
    assert _extract_text([]) == ""


def test_m3_extract_text_raises_on_blocks_with_no_text() -> None:
    """Pre-audit: silently returned ``""``, persisted as content='' in
    the DB. Now raises so the pipeline routes to its apology path."""

    class _Block:
        pass  # no .text — simulates an SDK shape we don't understand

    with pytest.raises(LLMHardFailure) as exc:
        _extract_text([_Block(), _Block()])
    assert "no extractable text" in str(exc.value)


# ----------------------------------------------------------------------
# H-3 — per-tenant persona honoured
# ----------------------------------------------------------------------


def test_h3_default_persona_renders_pol_polaris() -> None:
    """Backwards compatibility: callers that don't pass a persona get
    exactly the legacy ``Pol from Polaris`` template."""
    prompt = build_response_system_prompt("simple", "en")
    assert "Pol" in prompt
    assert "Polaris" in prompt


def test_h3_custom_persona_substitutes_assistant_and_company() -> None:
    """Per-tenant: portal-set assistant_name / company_name override
    the platform defaults via the persona kwarg."""
    persona = TenantPersona(
        assistant_name="Aria",
        company_name="Acme Industries",
    )
    prompt = build_response_system_prompt("simple", "en", persona)
    assert "Aria" in prompt
    assert "Acme Industries" in prompt
    # The platform default name must NOT leak through.
    assert "Pol from Polaris" not in prompt
    assert "I'm Pol" not in prompt


def test_h3_system_prompt_override_replaces_template_body() -> None:
    """A tenant that pre-rendered their own template via
    ``tenants.system_prompt`` gets it verbatim — the v1 platform
    template is dropped entirely (only language suffix + RAG
    injection guard wrap it)."""
    override = (
        "You are Bot, the assistant for ZetaCorp. Be terse. Reference "
        "the knowledge base only. Never mention competitors."
    )
    persona = TenantPersona(system_prompt_override=override)
    prompt = build_response_system_prompt("hard", "en", persona)
    assert override in prompt
    # Sanity: language suffix + brevity instruction + RAG guard still
    # appended for the hard tier.
    assert "<runtime_language>" in prompt
    assert "Keep it concise" in prompt
    assert "knowledge_chunk" in prompt
    # v1 template body markers must not appear.
    assert "Polaris" not in prompt or "Polaris" in override  # only if author wrote it


def test_h3_empty_string_overrides_treated_as_none() -> None:
    """Portal save coercing NULL → '' must NOT replace the template
    with an empty system prompt (which would break Anthropic's cache
    minimum + leave the model unguided)."""
    persona = TenantPersona(
        assistant_name="",
        company_name="",
        system_prompt_override="",
    )
    prompt = build_response_system_prompt("simple", "en", persona)
    # Falls back to platform defaults end-to-end.
    assert "Pol" in prompt
    assert "Polaris" in prompt


# ----------------------------------------------------------------------
# L-2 — media-only placeholder filtered from history
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l2_load_history_excludes_media_only_placeholder() -> None:
    """The ``[media-only message]`` placeholder rows that
    ``_emit_handoff_for_empty_body`` writes must not appear in the
    LLM-bound history window — Sonnet was observed echoing the literal
    string back to customers."""
    from polaris_api.services.conversation import _load_from_db

    conn = MagicMock()
    captured_sql: list[str] = []

    async def fetch(sql: str, *args: object) -> list[dict[str, object]]:
        captured_sql.append(sql)
        return []

    conn.fetch = AsyncMock(side_effect=fetch)

    await _load_from_db(conn, uuid4(), uuid4(), limit=20)

    assert len(captured_sql) == 1
    # The SELECT must include a filter against the placeholder.
    assert "[media-only message]" in captured_sql[0]
