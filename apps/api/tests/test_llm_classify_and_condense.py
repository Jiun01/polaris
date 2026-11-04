"""``classify_and_condense`` tests — audit Batch B (B4).

Covers the merged single-Haiku-call path that replaces sequential
``classify_intent`` + ``condense_followup_query`` for referential messages:
happy path (valid tool output), malformed-output fallback (degrades to
``classify_intent`` + original message), and stub mode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import make_mock_anthropic_message

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_tool_use_message(
    *, tier: str, query: str, tool_name: str = "route_message"
) -> MagicMock:
    """Build a MagicMock anthropic.types.Message with a tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = "toolu_test"
    block.name = tool_name
    block.input = {"tier": tier, "query": query}
    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock(
        input_tokens=150, output_tokens=25,
        cache_creation_input_tokens=100, cache_read_input_tokens=0,
    )
    return msg


_HISTORY = [
    {"role": "user", "content": "Do you have a KL outlet?"},
    {"role": "assistant", "content": "Yes, we have one in KL."},
]


async def test_happy_path_returns_tier_and_condensed_query(
    mock_anthropic: MagicMock,
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = _make_tool_use_message(
        tier="simple", query="KL outlet opening hours",
    )

    intent, query = await llm.classify_and_condense(_HISTORY, "what about the KL one?")

    assert intent == "simple"
    assert query == "KL outlet opening hours"
    # Exactly ONE Anthropic call for the merged path.
    assert mock_anthropic.messages.create.await_count == 1
    kwargs = mock_anthropic.messages.create.call_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "route_message"}


async def test_malformed_tool_output_falls_back_to_classify_intent(
    mock_anthropic: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No route_message tool_use block in the response → falls back to
    classify_intent + original message."""
    from polaris_api.services import llm

    # First call (the merged attempt) returns plain text, no tool_use block.
    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="not a tool call",
    )
    fallback_mock = AsyncMock(return_value="medium")
    monkeypatch.setattr(llm, "classify_intent", fallback_mock)

    intent, query = await llm.classify_and_condense(_HISTORY, "what about the KL one?")

    assert intent == "medium"
    assert query == "what about the KL one?"
    fallback_mock.assert_awaited_once()


async def test_invalid_tier_falls_back_to_classify_intent(
    mock_anthropic: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool_use block with an out-of-enum tier degrades to the fallback path."""
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = _make_tool_use_message(
        tier="not-a-real-tier", query="KL outlet hours",
    )
    fallback_mock = AsyncMock(return_value="hard")
    monkeypatch.setattr(llm, "classify_intent", fallback_mock)

    intent, query = await llm.classify_and_condense(_HISTORY, "what about the KL one?")

    assert intent == "hard"
    assert query == "what about the KL one?"
    fallback_mock.assert_awaited_once()


async def test_anthropic_error_falls_back_to_classify_intent(
    mock_anthropic: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any Anthropic-call exception degrades to the sequential fallback path —
    never a dropped reply."""
    from polaris_api.services import llm

    mock_anthropic.messages.create.side_effect = RuntimeError("boom")
    fallback_mock = AsyncMock(return_value="simple")
    monkeypatch.setattr(llm, "classify_intent", fallback_mock)

    intent, query = await llm.classify_and_condense(_HISTORY, "what about the KL one?")

    assert intent == "simple"
    assert query == "what about the KL one?"
    fallback_mock.assert_awaited_once()


async def test_stub_mode_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """llm_stub_enabled=True → deterministic stub, no network call."""
    from polaris_api.config import get_settings
    from polaris_api.services import llm

    monkeypatch.setenv("LLM_STUB_ENABLED", "true")
    get_settings.cache_clear()

    intent, query = await llm.classify_and_condense(_HISTORY, "I need a human agent")

    assert intent == "handoff"
    assert query == "I need a human agent"

    get_settings.cache_clear()


async def test_empty_history_falls_back_without_extra_call(
    mock_anthropic: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No conversation history → classify_intent alone (no merged call)."""
    from polaris_api.services import llm

    fallback_mock = AsyncMock(return_value="medium")
    monkeypatch.setattr(llm, "classify_intent", fallback_mock)

    intent, query = await llm.classify_and_condense([], "hello")

    assert intent == "medium"
    assert query == "hello"
    fallback_mock.assert_awaited_once()
    mock_anthropic.messages.create.assert_not_called()


async def test_overlong_query_falls_back_to_original_message(
    mock_anthropic: MagicMock,
) -> None:
    """A rewritten query over 300 chars is discarded in favor of the
    original message (mirrors condense_followup_query's guard)."""
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = _make_tool_use_message(
        tier="medium", query="x" * 400,
    )

    intent, query = await llm.classify_and_condense(_HISTORY, "what about the KL one?")

    assert intent == "medium"
    assert query == "what about the KL one?"
