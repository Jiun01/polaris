"""generate_response tests — Phase 4 PR-1.

Covers model/max_tokens routing per intent tier, language detection
folded into the system prompt, ResponseEnvelope shape, and the
handoff-misuse guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from tests.conftest import make_mock_anthropic_message

pytestmark = pytest.mark.asyncio


async def test_simple_intent_routes_to_haiku_500_tokens(
    mock_anthropic: MagicMock,
) -> None:
    from polaris_api.services import llm

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="what time you open",
        intent="simple",
    )

    kwargs = mock_anthropic.messages.create.call_args.kwargs
    assert "haiku" in kwargs["model"].lower()
    assert kwargs["max_tokens"] == 500
    assert envelope.model_used == "haiku"
    assert envelope.intent == "simple"


async def test_medium_intent_routes_to_haiku_800_tokens(
    mock_anthropic: MagicMock,
) -> None:
    from polaris_api.services import llm

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="what's the diff between plan A and plan B",
        intent="medium",
    )

    kwargs = mock_anthropic.messages.create.call_args.kwargs
    assert "haiku" in kwargs["model"].lower()
    assert kwargs["max_tokens"] == 800
    assert envelope.model_used == "haiku"


async def test_hard_intent_routes_to_sonnet_1000_tokens_streamed(
    mock_anthropic: MagicMock,
) -> None:
    """Hard tier uses Sonnet via the STREAM path, not create."""
    from polaris_api.services import llm

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="please walk me through how to migrate my account",
        intent="hard",
    )

    # Non-stream create should NOT have been called for hard tier.
    mock_anthropic.messages.create.assert_not_called()
    # Stream WAS called with sonnet model + 1000 tokens.
    stream_kwargs = mock_anthropic.messages.stream.call_args.kwargs
    assert "sonnet" in stream_kwargs["model"].lower()
    assert stream_kwargs["max_tokens"] == 1000
    assert envelope.model_used == "sonnet"
    assert envelope.intent == "hard"


async def test_handoff_intent_rejected_by_generate_response() -> None:
    """generate_response refuses intent='handoff' (caller bug)."""
    from polaris_api.services import llm

    with pytest.raises(ValueError, match="handoff"):
        await llm.generate_response(
            tenant_id=uuid4(),
            rag_context="",
            conversation_history=[],
            user_message="get me a human",
            intent="handoff",
        )


async def test_response_envelope_shape_full_telemetry(
    mock_anthropic: MagicMock,
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="hello there",
        input_tokens=120,
        output_tokens=15,
        cache_creation_input_tokens=80,
        cache_read_input_tokens=0,
    )

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="hi",
        intent="simple",
    )

    assert envelope.content == "hello there"
    assert envelope.usage.input_tokens == 120
    assert envelope.usage.output_tokens == 15
    assert envelope.usage.cache_creation_tokens == 80
    assert envelope.usage.cache_read_tokens == 0
    assert envelope.usage.cache_hit_rate == 0.0
    assert envelope.latency_ms >= 0
    assert envelope.fallback_used is False
    assert envelope.fallback_reason is None


async def test_cache_hit_rate_on_second_message(
    mock_anthropic: MagicMock,
) -> None:
    """Phase 4 exit criterion 1: hit rate > 0 on the second message."""
    from polaris_api.services import llm

    # Second message: 100 input tokens, 90 served from cache.
    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="ok",
        input_tokens=100,
        output_tokens=10,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=90,
    )

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "ok"},
        ],
        user_message="follow-up",
        intent="simple",
    )

    assert envelope.usage.cache_read_tokens == 90
    assert envelope.usage.cache_hit_rate == 0.9


async def test_conversation_history_passed_through(
    mock_anthropic: MagicMock,
) -> None:
    """History dicts appear in messages before the final user turn."""
    from polaris_api.services import llm

    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=history,
        user_message="final",
        intent="simple",
    )

    messages = mock_anthropic.messages.create.call_args.kwargs["messages"]
    assert len(messages) == 5  # 4 history + 1 final user
    assert messages[0]["content"] == "q1"
    assert messages[3]["content"] == "a2"


async def test_language_detection_chinese_threads_into_system_prompt(
    mock_anthropic: MagicMock,
) -> None:
    """CJK input → zh-cn in envelope; system prompt mentions Chinese."""
    from polaris_api.services import llm

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="你好，请问几点开门？",
        intent="simple",
    )

    assert envelope.language_detected == "zh-cn"
    system_text = mock_anthropic.messages.create.call_args.kwargs["system"][0]["text"]
    assert "Chinese" in system_text


async def test_language_detection_malay_threads_into_system_prompt(
    mock_anthropic: MagicMock,
) -> None:
    """Malay stopword density → ms in envelope; system prompt mentions Malay."""
    from polaris_api.services import llm

    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        # 2+ stopwords: dengan, untuk
        user_message="saya nak buat tempahan untuk hari esok dengan anda",
        intent="simple",
    )

    assert envelope.language_detected == "ms"
    system_text = mock_anthropic.messages.create.call_args.kwargs["system"][0]["text"]
    assert "Malaysia" in system_text


async def test_rag_chunk_ids_threaded_into_envelope(
    mock_anthropic: MagicMock,
) -> None:
    """Pass chunks; ids surface on the envelope for Phase 8 sources panel."""
    from polaris_api.services import llm
    from polaris_shared_types import KnowledgeChunkHit

    chunks = [
        KnowledgeChunkHit(
            id=uuid4(),
            content="fact 1",
            source_name="faq.md",
            rrf_score=0.9,
            vector_rank=1,
            bm25_rank=1,
            distance=None,
        ),
        KnowledgeChunkHit(
            id=uuid4(),
            content="fact 2",
            source_name="hours.md",
            rrf_score=0.7,
            vector_rank=2,
            bm25_rank=2,
            distance=None,
        ),
    ]
    envelope = await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="<knowledge_chunk>fact 1</knowledge_chunk>",
        conversation_history=[],
        user_message="when do you open?",
        intent="simple",
        chunks=chunks,
    )

    assert envelope.rag_chunk_ids == [str(chunks[0].id), str(chunks[1].id)]
