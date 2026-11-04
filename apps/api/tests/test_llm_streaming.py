"""Streaming-path tests — Phase 4 PR-1.

Sonnet hard tier uses ``messages.stream(...)`` per deep-dive §5.1.
Haiku tiers stay non-stream. On stream error, the wrapper retries
once non-streaming with the same model before escalating to the
OpenRouter fallback chain.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import anthropic
import pytest

from tests.conftest import FakeAnthropicStream, make_mock_anthropic_message

pytestmark = pytest.mark.asyncio


async def test_sonnet_hard_uses_stream_not_create(
    mock_anthropic: MagicMock,
) -> None:
    """Hard intent → stream() invoked, create() NOT invoked."""
    from polaris_api.services import llm

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="complex multi-step question",
        intent="hard",
    )

    mock_anthropic.messages.stream.assert_called_once()
    mock_anthropic.messages.create.assert_not_called()


async def test_haiku_tiers_stay_nonstreaming(
    mock_anthropic: MagicMock,
) -> None:
    """Simple + medium use create(), never stream()."""
    from polaris_api.services import llm

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="simple q",
        intent="simple",
    )
    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="medium q",
        intent="medium",
    )

    assert mock_anthropic.messages.create.call_count == 2
    mock_anthropic.messages.stream.assert_not_called()


async def test_stream_consumes_text_chunks_and_returns_final(
    mock_anthropic: MagicMock,
) -> None:
    """The stream's text_stream is drained; get_final_message() drives content + usage."""
    from polaris_api.services import llm

    final = make_mock_anthropic_message(
        text="full sonnet answer",
        input_tokens=300,
        output_tokens=80,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=200,
    )
    mock_anthropic.messages.stream.return_value = FakeAnthropicStream(
        chunks=["full ", "sonnet ", "answer"], final=final,
    )

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="hard q",
        intent="hard",
    )

    assert envelope.content == "full sonnet answer"
    assert envelope.usage.input_tokens == 300
    assert envelope.usage.output_tokens == 80
    assert envelope.usage.cache_read_tokens == 200
    assert envelope.model_used == "sonnet"


async def test_stream_error_falls_back_to_nonstream_create(
    mock_anthropic: MagicMock,
) -> None:
    """If stream errors mid-flight, retry once with create() before escalating."""
    from polaris_api.services import llm

    # Stream raises mid-iteration; non-stream create returns success.
    request = MagicMock()
    bad_stream = FakeAnthropicStream(
        chunks=["partial "],
        final=make_mock_anthropic_message(text="should not reach"),
        raise_in_iter=anthropic.APIConnectionError(request=request),
    )
    mock_anthropic.messages.stream.return_value = bad_stream
    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="recovered via nonstream",
        input_tokens=250, output_tokens=40,
    )

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="hard q",
        intent="hard",
    )

    assert envelope.content == "recovered via nonstream"
    assert envelope.model_used == "sonnet"
    assert envelope.fallback_used is False  # internal retry, not provider switch
    mock_anthropic.messages.stream.assert_called_once()
    mock_anthropic.messages.create.assert_called_once()


async def test_stream_path_passes_cache_control_correctly(
    mock_anthropic: MagicMock,
) -> None:
    """Streaming path must use the same cache_control shape as non-stream."""
    from polaris_api.services import llm

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="<knowledge_chunk>fact</knowledge_chunk>",
        conversation_history=[],
        user_message="hard",
        intent="hard",
    )

    stream_kwargs = mock_anthropic.messages.stream.call_args.kwargs
    # Per-block cache_control on system, no top-level kwarg, no leak into messages.
    assert "cache_control" not in stream_kwargs
    system = stream_kwargs["system"]
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    for msg in stream_kwargs["messages"]:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                assert "cache_control" not in block
