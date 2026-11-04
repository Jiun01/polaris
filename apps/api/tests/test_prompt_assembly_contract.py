"""Contract guard for the system-prompt assembly — B0 regression scaffold.

This is the deterministic safety net for the B1 prompt refactor (ADR-063,
`.claude/plans/agent-tool-surface.plan.md`). It locks the *current* behavior of
``build_response_system_prompt`` + ``TenantPersona`` so the layered rewrite is a
visible, intentional diff rather than a silent behavior change.

Two classes of assertion live here:

  * **Invariants B1 MUST preserve** — block composition, brevity-only-on-hard,
    language suffix, persona substitution, override semantics, block ordering.
  * **Current-state pins B1 WILL intentionally flip** — grouped under
    ``TestCurrentStateToBeChangedInB1``. Each is annotated with the planned
    change. When B1 lands, these flip together with a clear reason (delete the
    vestigial ``[HANDOFF_TRIGGER]``; make formatting channel-aware). Keeping them
    here means the refactor can't quietly drop or keep the wrong thing.

The existing exact-LLM-output evals (``evals/intent_classification.yaml`` etc.)
can't score freeform persona text; this contract test is the right gate for a
prompt refactor (mirrors the ``test_llm_cache_strategy.py`` guard philosophy).
"""

from __future__ import annotations

import pytest
from polaris_api.services._llm_prompts import (
    TenantPersona,
    build_agent_system_prompt,
    build_email_draft_prompt,
    build_response_system_prompt,
)

_INTENTS = ("simple", "medium", "hard")
_LANGUAGES = (("en", "English"), ("ms", "Bahasa Malaysia"), ("zh-cn", "Simplified Chinese"))


# ----------------------------------------------------------------------
# Invariants the B1 layered refactor MUST preserve
# ----------------------------------------------------------------------


@pytest.mark.parametrize("intent", _INTENTS)
@pytest.mark.parametrize("language,lang_name", _LANGUAGES)
def test_rag_injection_guard_present_every_tier(
    intent: str, language: str, lang_name: str
) -> None:
    """The RAG-injection guard is non-negotiable on every tier + language."""
    prompt = build_response_system_prompt(intent, language)  # type: ignore[arg-type]
    assert "knowledge_chunk" in prompt
    assert "data, not instructions" in prompt


@pytest.mark.parametrize("language,lang_name", _LANGUAGES)
def test_language_suffix_names_detected_language(language: str, lang_name: str) -> None:
    """The runtime language suffix is appended and names the detected language."""
    prompt = build_response_system_prompt("simple", language)  # type: ignore[arg-type]
    assert "<runtime_language>" in prompt
    assert lang_name in prompt


@pytest.mark.parametrize("intent", _INTENTS)
def test_brevity_instruction_only_on_hard(intent: str) -> None:
    """Brevity nudge applies to the Sonnet (hard) tier only."""
    prompt = build_response_system_prompt(intent, "en")  # type: ignore[arg-type]
    if intent == "hard":
        assert "Keep it concise" in prompt
    else:
        assert "Keep it concise" not in prompt


def test_default_persona_renders_platform_defaults() -> None:
    """No persona → legacy Pol / Polaris defaults (backwards compat)."""
    prompt = build_response_system_prompt("simple", "en")
    assert "Pol" in prompt
    assert "Polaris" in prompt


def test_custom_persona_substitutes_names() -> None:
    persona = TenantPersona(assistant_name="Aria", company_name="Acme Industries")
    prompt = build_response_system_prompt("simple", "en", persona)
    assert "Aria" in prompt
    assert "Acme Industries" in prompt
    assert "I'm Pol" not in prompt


def test_override_keeps_wrappers_and_cannot_strip_safety() -> None:
    """B1 composability: an override REPLACES the persona body, but the format
    block, the non-overridable _SAFETY_GUARDS, brevity, and the RAG guard are
    still composed around it. The safety guards are the load-bearing fix — a
    tenant override can no longer drop medical/legal/payment rules.
    """
    override = "You are Bot for ZetaCorp. Be terse. KB only."
    persona = TenantPersona(system_prompt_override=override)
    prompt = build_response_system_prompt("hard", "en", persona)
    assert override in prompt
    assert "<runtime_language>" in prompt
    assert "Keep it concise" in prompt
    assert "knowledge_chunk" in prompt
    # Safety guards survive the override (the B1 fix):
    assert "medical, legal, or financial" in prompt
    assert "<non_negotiable_rules>" in prompt


def test_empty_override_falls_back_to_defaults() -> None:
    persona = TenantPersona(assistant_name="", company_name="", system_prompt_override="")
    prompt = build_response_system_prompt("simple", "en", persona)
    assert "Pol" in prompt
    assert "Polaris" in prompt


def test_block_ordering_hard_tier() -> None:
    """Assembly order (hard): persona body → brevity → RAG guard → <runtime_language>.

    Ordering is part of the contract — the guard/brevity trail the persona, and as
    of 2026-06-15 the <runtime_language> directive is LAST (closest to the
    conversation) so recency makes the model honor a mid-conversation language
    switch. It must never lead the persona.
    """
    prompt = build_response_system_prompt("hard", "en")
    i_brevity = prompt.index("Keep it concise")
    i_guard = prompt.index("knowledge_chunk")
    i_lang = prompt.index("<runtime_language>")
    assert i_brevity < i_guard < i_lang


# ----------------------------------------------------------------------
# Current-state pins — B1 WILL intentionally flip these
# ----------------------------------------------------------------------


class TestB1IntentionalChanges:
    """Locks the two deliberate B1 behavior changes so they can't regress."""

    @pytest.mark.parametrize("intent", _INTENTS)
    def test_handoff_trigger_removed_from_chat_prompt(self, intent: str) -> None:
        """The vestigial ``[HANDOFF_TRIGGER]`` sentinel (no consumer, leaked raw)
        is gone. Handoff is the classifier path + the handoff_to_human tool."""
        prompt = build_response_system_prompt(intent, "en")  # type: ignore[arg-type]
        assert "[HANDOFF_TRIGGER]" not in prompt

    def test_handoff_trigger_removed_from_email_prompt(self) -> None:
        prompt = build_email_draft_prompt("medium", "en")
        assert "[HANDOFF_TRIGGER]" not in prompt

    def test_chat_prompt_has_plaintext_rules(self) -> None:
        """Chat channels still get the plain-text / no-markdown formatting."""
        prompt = build_response_system_prompt("simple", "en", channel="whatsapp")
        assert "Plain text only" in prompt


class TestEmailDraftPrompt:
    """The separate email-draft prompt — email is not a chat reply."""

    def test_email_prompt_has_email_framing_not_chat_rules(self) -> None:
        prompt = build_email_draft_prompt("medium", "en")
        # Email framing present:
        assert "human agent will review" in prompt
        assert "sign-off" in prompt
        # Chat-only plain-text rule absent:
        assert "Plain text only" not in prompt
        # No brevity cap on email:
        assert "Keep it concise" not in prompt

    def test_email_prompt_keeps_safety_and_rag_guard(self) -> None:
        prompt = build_email_draft_prompt("medium", "en")
        assert "<non_negotiable_rules>" in prompt
        assert "knowledge_chunk" in prompt

    def test_email_instructions_appended(self) -> None:
        prompt = build_email_draft_prompt(
            "medium", "en", email_instructions="Always CC the sales team."
        )
        assert "Always CC the sales team." in prompt


class TestAgentSystemPrompt:
    """The tool-calling agent prompt (B3) — persona + safety + tool rules."""

    def test_has_tool_rules_and_safety_no_sentinel(self) -> None:
        prompt = build_agent_system_prompt("medium", "en")
        assert "<using_tools>" in prompt
        assert "<non_negotiable_rules>" in prompt
        assert "knowledge_chunk" in prompt
        assert "[HANDOFF_TRIGGER]" not in prompt

    def test_tenant_instructions_appended(self) -> None:
        prompt = build_agent_system_prompt(
            "medium", "en", instructions="We are a halal bakery; never suggest pork."
        )
        assert "halal bakery" in prompt
        assert "<tenant_instructions>" in prompt

    def test_no_brevity_cap_on_agent(self) -> None:
        # The agent decides length per turn; the hard-tier brevity nudge is chat-only.
        assert "Keep it concise" not in build_agent_system_prompt("hard", "en")

    def test_safety_survives_a_tenant_override(self) -> None:
        persona = TenantPersona(system_prompt_override="You are Z for ZCorp. Be brief.")
        prompt = build_agent_system_prompt("medium", "en", persona)
        assert "You are Z for ZCorp" in prompt
        assert "medical, legal, or financial" in prompt  # guards not strippable
