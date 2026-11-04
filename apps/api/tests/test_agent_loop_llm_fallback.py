"""generate_with_tools resilience — silent-failure audit fix A1 (2026-07-02).

Mirrors test_llm_fallback.py's trigger/assert shape but for the tool-use path:
Anthropic transient error -> OpenRouter fallback (no tools) -> LLMHardFailure
if both fail. Also covers pipeline._try_agent_runtime catching LLMHardFailure
and falling through (result=None) instead of propagating.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import anthropic
import openai
import pytest

pytestmark = pytest.mark.asyncio


def _make_anthropic_503() -> anthropic.InternalServerError:
    response = MagicMock(status_code=503)
    return anthropic.InternalServerError("upstream 503", response=response, body=None)


def _make_openai_503() -> openai.InternalServerError:
    response = MagicMock(status_code=503)
    return openai.InternalServerError("upstream 503", response=response, body=None)


async def test_generate_with_tools_falls_back_to_openrouter(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.side_effect = _make_anthropic_503()

    turn = await llm.generate_with_tools(
        system_prompt="sys", messages=[{"role": "user", "content": "hi"}], tools=[],
    )
    assert turn.stop_reason == "end_turn"
    assert turn.tool_calls == ()
    mock_openrouter.chat.completions.create.assert_called_once()
    # Tools must NOT be forwarded to OpenRouter.
    or_kwargs = mock_openrouter.chat.completions.create.call_args.kwargs
    assert "tools" not in or_kwargs


async def test_generate_with_tools_degrades_non_string_message_content(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    """A prior assistant/tool_result turn with list content must not crash the
    fallback — it degrades to a short marker instead of a malformed OpenAI
    message shape."""
    from polaris_api.services import llm

    mock_anthropic.messages.create.side_effect = _make_anthropic_503()

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "tool call"}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": []}],
        },
    ]
    await llm.generate_with_tools(system_prompt="sys", messages=messages, tools=[])

    or_kwargs = mock_openrouter.chat.completions.create.call_args.kwargs
    contents = [m["content"] for m in or_kwargs["messages"]]
    assert "[tool exchange omitted]" in contents


async def test_generate_with_tools_double_failure_raises_hard_failure(
    mock_anthropic: MagicMock, mock_openrouter: MagicMock,
) -> None:
    from polaris_api.services import llm
    from polaris_api.services._llm_models import LLMHardFailure

    mock_anthropic.messages.create.side_effect = _make_anthropic_503()
    mock_openrouter.chat.completions.create.side_effect = _make_openai_503()

    with pytest.raises(LLMHardFailure):
        await llm.generate_with_tools(
            system_prompt="sys", messages=[{"role": "user", "content": "hi"}], tools=[],
        )


async def test_try_agent_runtime_catches_llm_hard_failure_and_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLMHardFailure from the agent loop must fall through (None), not crash
    the inbound handler."""
    from polaris_api.services import pipeline
    from polaris_api.services._llm_models import LLMHardFailure
    from polaris_api.services.agent import pipeline_integration as agent_integration
    from polaris_shared_types import IncomingMessage, SenderRef

    async def _raise(*_a: object, **_kw: object) -> None:
        raise LLMHardFailure("both providers down")

    monkeypatch.setattr(agent_integration, "try_run_agent", _raise)

    incoming = IncomingMessage(
        tenant_id=uuid4(),
        channel_id=uuid4(),
        provider="whatsapp",
        provider_message_id="wamid.test",
        sender=SenderRef(channel_user_id="60123"),
        body="hi",
        received_at=datetime.now(UTC),
    )
    deps = pipeline.PipelineDeps(
        anon_pool=MagicMock(), redis=AsyncMock(), meta_client=MagicMock(),
    )

    result = await pipeline._try_agent_runtime(
        conn=AsyncMock(),
        deps=deps,
        incoming=incoming,
        conversation_id=uuid4(),
        user_msg_id=uuid4(),
        t0=time.monotonic(),
        channel="whatsapp",
    )
    assert result is None
