"""Fallback-path tests — Phase 4 PR-1.

Per ADR-031 Amendment 2026-05-15: Anthropic transient failures
(5xx / timeout / rate-limit / connection) fall back to OpenRouter
``openai/gpt-4o-mini`` UNCACHED. Both providers failing surfaces as
LLMHardFailure (caller's apology-reply path).

Tests cover: trigger conditions, OpenRouter invocation shape, envelope
flags (fallback_used + fallback_reason), and the double-failure
escalation.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import anthropic
import openai
import pytest

pytestmark = pytest.mark.asyncio


def _make_anthropic_503() -> anthropic.InternalServerError:
    response = MagicMock(status_code=503)
    return anthropic.InternalServerError("upstream 503", response=response, body=None)


def _make_anthropic_timeout() -> anthropic.APITimeoutError:
    request = MagicMock()
    return anthropic.APITimeoutError(request=request)


def _make_openai_503() -> openai.InternalServerError:
    response = MagicMock(status_code=503)
    return openai.InternalServerError("upstream 503", response=response, body=None)


async def test_anthropic_500_falls_back_to_openrouter(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.side_effect = _make_anthropic_503()

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="hello",
        intent="simple",
    )

    assert envelope.fallback_used is True
    assert envelope.fallback_reason and "InternalServerError" in envelope.fallback_reason
    assert envelope.model_used == "gpt-4o-mini-fallback"
    mock_openrouter.chat.completions.create.assert_called_once()


async def test_anthropic_timeout_falls_back_to_openrouter(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.side_effect = _make_anthropic_timeout()

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="hi",
        intent="medium",
    )

    assert envelope.fallback_used is True
    assert envelope.model_used == "gpt-4o-mini-fallback"
    assert "Timeout" in (envelope.fallback_reason or "")


async def test_fallback_call_targets_openrouter_with_gpt_4o_mini(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    """OpenRouter fallback uses ``openai/gpt-4o-mini`` model id."""
    from polaris_api.services import llm

    mock_anthropic.messages.create.side_effect = _make_anthropic_503()

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="kb stuff",
        conversation_history=[],
        user_message="hi",
        intent="simple",
    )

    or_kwargs = mock_openrouter.chat.completions.create.call_args.kwargs
    assert or_kwargs["model"] == "openai/gpt-4o-mini"
    # Max-tokens routing carried over (simple → 500).
    assert or_kwargs["max_tokens"] == 500
    # First message is system; should include the RAG context inlined.
    assert or_kwargs["messages"][0]["role"] == "system"
    assert "kb stuff" in or_kwargs["messages"][0]["content"]


async def test_fallback_envelope_marks_zero_cache_tokens(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    """OpenRouter is UNCACHED per ADR-031 Amendment — assert cache fields are 0."""
    from polaris_api.services import llm

    mock_anthropic.messages.create.side_effect = _make_anthropic_503()

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="hi",
        intent="simple",
    )

    assert envelope.usage.cache_creation_tokens == 0
    assert envelope.usage.cache_read_tokens == 0
    assert envelope.usage.cache_hit_rate == 0.0


async def test_double_failure_raises_hard_failure(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    """Anthropic AND OpenRouter both transient → LLMHardFailure."""
    from polaris_api.services import llm
    from polaris_api.services._llm_models import LLMHardFailure

    mock_anthropic.messages.create.side_effect = _make_anthropic_503()
    mock_openrouter.chat.completions.create.side_effect = _make_openai_503()

    with pytest.raises(LLMHardFailure):
        await llm.generate_response(
            tenant_id=uuid4(),
            rag_context="",
            conversation_history=[],
            user_message="hi",
            intent="simple",
        )


async def test_anthropic_4xx_validation_error_does_not_fall_back(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    """4xx validation/auth errors are bugs, not transient. No fallback."""
    from polaris_api.services import llm

    request = MagicMock()
    response = MagicMock(status_code=400, request=request)
    mock_anthropic.messages.create.side_effect = anthropic.BadRequestError(
        "bad shape", response=response, body=None,
    )

    with pytest.raises(anthropic.BadRequestError):
        await llm.generate_response(
            tenant_id=uuid4(),
            rag_context="",
            conversation_history=[],
            user_message="hi",
            intent="simple",
        )
    mock_openrouter.chat.completions.create.assert_not_called()


async def test_fallback_requires_openrouter_key(
    mock_anthropic: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If OPENROUTER_API_KEY is unset, fallback raises LLMConfigError."""
    from polaris_api.config import get_settings
    from polaris_api.services import llm
    from polaris_api.services._llm_models import LLMConfigError

    # Anthropic fails; OpenRouter has no real key.
    mock_anthropic.messages.create.side_effect = _make_anthropic_503()
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-openrouter-key")
    get_settings.cache_clear()
    llm._reset_clients_for_testing()
    monkeypatch.setattr(llm, "_get_anthropic_client", lambda: mock_anthropic)

    with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY"):
        await llm.generate_response(
            tenant_id=uuid4(),
            rag_context="",
            conversation_history=[],
            user_message="hi",
            intent="simple",
        )
