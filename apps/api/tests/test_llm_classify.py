"""Intent-classifier tests — Phase 4 PR-1.

Covers ``services/llm.py::classify_intent``: tier mapping,
unparseable-response fallback, and the LLMConfigError path on an
unset/placeholder API key.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import pytest

from tests.conftest import make_mock_anthropic_message

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("simple", "simple"),
        ("medium", "medium"),
        ("hard", "hard"),
        ("handoff", "handoff"),
        # whitespace + casing tolerated (Haiku sometimes emits "  Hard\n")
        ("  Hard\n", "hard"),
        ("HANDOFF", "handoff"),
    ],
)
async def test_classify_returns_known_tier(
    mock_anthropic: MagicMock, raw: str, expected: str
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text=raw,
    )
    intent = await llm.classify_intent("any text")
    assert intent == expected


async def test_classify_unparseable_falls_back_to_medium(
    mock_anthropic: MagicMock,
) -> None:
    """Garbage output → ``medium`` (safer than ``simple``).

    Medium still uses Haiku but with an 800-tok cap, so a
    misclassified hard question at least gets a fuller answer
    attempt. Logged as warning for monitoring.
    """
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="this is not a valid intent",
    )
    intent = await llm.classify_intent("ambiguous input")
    assert intent == "medium"


async def test_classify_empty_content_falls_back_to_medium(
    mock_anthropic: MagicMock,
) -> None:
    """Empty SDK content array → medium fallback (no crash)."""
    from polaris_api.services import llm

    empty = MagicMock()
    empty.content = []
    empty.usage = MagicMock(
        input_tokens=10, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    mock_anthropic.messages.create.return_value = empty

    intent = await llm.classify_intent("input")
    assert intent == "medium"


async def test_classify_uses_haiku_model_and_small_max_tokens(
    mock_anthropic: MagicMock,
) -> None:
    """Classifier uses the Haiku model + 10-token budget (one word)."""
    from polaris_api.services import llm

    await llm.classify_intent("hello")

    kwargs = mock_anthropic.messages.create.call_args.kwargs
    # Default config: LLM_HAIKU_MODEL=claude-haiku-4-5-20251001
    assert "haiku" in kwargs["model"].lower()
    assert kwargs["max_tokens"] == 10


async def test_classify_raises_config_error_on_placeholder_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``placeholder-*`` key → fail-fast LLMConfigError, no network call."""
    from polaris_api.config import get_settings
    from polaris_api.services import llm
    from polaris_api.services._llm_models import LLMConfigError

    # Force a fresh settings + clear client cache so the placeholder
    # guard runs on this call.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-anthropic-key")
    get_settings.cache_clear()
    llm._reset_clients_for_testing()

    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        await llm.classify_intent("hello")


async def test_classify_raises_transient_on_anthropic_500(
    mock_anthropic: MagicMock,
) -> None:
    """Anthropic 5xx → LLMTransientError (caller decides what to do)."""
    from polaris_api.services import llm
    from polaris_api.services._llm_models import LLMTransientError

    response = MagicMock(status_code=500)
    mock_anthropic.messages.create.side_effect = anthropic.InternalServerError(
        "boom", response=response, body=None,
    )

    with pytest.raises(LLMTransientError):
        await llm.classify_intent("hello")
