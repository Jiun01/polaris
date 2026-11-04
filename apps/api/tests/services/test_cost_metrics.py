"""Cost-metric emission tests — feeds the admin cost dashboard.

The dashboard (routers/admin_cost.py → services/grafana_cost.py) reads
``polaris_anthropic_{input,output}_tokens_total`` and
``polaris_cohere_rerank_calls_total`` from Mimir. These tests lock the
emission side: that the request path records those counters with the
``tenant_id`` / ``intent_tier`` / ``model_used`` labels the PromQL groups by.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from polaris_api import metrics

from tests.conftest import make_mock_anthropic_message

# ---------------------------------------------------------------------------
# Unit: the record_* helpers
# ---------------------------------------------------------------------------


def test_record_anthropic_usage_emits_both_counters_with_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_counter = MagicMock()
    out_counter = MagicMock()
    monkeypatch.setattr(metrics, "_anthropic_input_tokens", in_counter)
    monkeypatch.setattr(metrics, "_anthropic_output_tokens", out_counter)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    tid = uuid4()
    metrics.record_anthropic_usage(
        tenant_id=tid,
        intent_tier="hard",
        model_used="sonnet",
        input_tokens=1200,
        output_tokens=340,
    )

    in_counter.add.assert_called_once()
    amount, attrs = in_counter.add.call_args.args
    assert amount == 1200
    assert attrs == {
        "tenant_id": str(tid),
        "intent_tier": "hard",
        "model_used": "sonnet",
    }
    out_amount, out_attrs = out_counter.add.call_args.args
    assert out_amount == 340
    assert out_attrs["model_used"] == "sonnet"


def test_record_anthropic_usage_skips_zero_token_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_counter = MagicMock()
    out_counter = MagicMock()
    monkeypatch.setattr(metrics, "_anthropic_input_tokens", in_counter)
    monkeypatch.setattr(metrics, "_anthropic_output_tokens", out_counter)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    metrics.record_anthropic_usage(
        tenant_id=uuid4(),
        intent_tier="simple",
        model_used="haiku",
        input_tokens=0,
        output_tokens=0,
    )

    in_counter.add.assert_not_called()
    out_counter.add.assert_not_called()


def test_record_anthropic_usage_emits_cache_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_counter = MagicMock()
    read_counter = MagicMock()
    monkeypatch.setattr(metrics, "_anthropic_input_tokens", MagicMock())
    monkeypatch.setattr(metrics, "_anthropic_output_tokens", MagicMock())
    monkeypatch.setattr(metrics, "_anthropic_cache_write_tokens", write_counter)
    monkeypatch.setattr(metrics, "_anthropic_cache_read_tokens", read_counter)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    tid = uuid4()
    metrics.record_anthropic_usage(
        tenant_id=tid,
        intent_tier="medium",
        model_used="haiku",
        input_tokens=300,
        output_tokens=80,
        cache_write_tokens=2000,
        cache_read_tokens=5000,
    )

    w_amount, w_attrs = write_counter.add.call_args.args
    assert w_amount == 2000
    assert w_attrs == {
        "tenant_id": str(tid),
        "intent_tier": "medium",
        "model_used": "haiku",
    }
    r_amount, _ = read_counter.add.call_args.args
    assert r_amount == 5000


def test_record_anthropic_usage_skips_zero_cache_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_counter = MagicMock()
    read_counter = MagicMock()
    monkeypatch.setattr(metrics, "_anthropic_input_tokens", MagicMock())
    monkeypatch.setattr(metrics, "_anthropic_output_tokens", MagicMock())
    monkeypatch.setattr(metrics, "_anthropic_cache_write_tokens", write_counter)
    monkeypatch.setattr(metrics, "_anthropic_cache_read_tokens", read_counter)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    # Default (uncached) reply — no cache tokens, so no cache series churn.
    metrics.record_anthropic_usage(
        tenant_id=uuid4(),
        intent_tier="simple",
        model_used="haiku",
        input_tokens=300,
        output_tokens=80,
    )

    write_counter.add.assert_not_called()
    read_counter.add.assert_not_called()


def test_record_cohere_rerank_emits_with_tenant_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = MagicMock()
    monkeypatch.setattr(metrics, "_cohere_rerank_calls", counter)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    tid = uuid4()
    metrics.record_cohere_rerank(tenant_id=tid)

    amount, attrs = counter.add.call_args.args
    assert amount == 1
    assert attrs == {"tenant_id": str(tid)}


def test_record_openrouter_embedding_emits_with_tenant_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = MagicMock()
    monkeypatch.setattr(metrics, "_openrouter_embedding_tokens", counter)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    tid = uuid4()
    metrics.record_openrouter_embedding(tenant_id=tid, tokens=42)

    amount, attrs = counter.add.call_args.args
    assert amount == 42
    assert attrs == {"tenant_id": str(tid)}


def test_record_openrouter_embedding_skips_zero_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = MagicMock()
    monkeypatch.setattr(metrics, "_openrouter_embedding_tokens", counter)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    metrics.record_openrouter_embedding(tenant_id=uuid4(), tokens=0)

    counter.add.assert_not_called()


def test_record_helpers_are_noops_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_counter = MagicMock()
    counter = MagicMock()
    monkeypatch.setattr(metrics, "_anthropic_input_tokens", in_counter)
    monkeypatch.setattr(metrics, "_cohere_rerank_calls", counter)
    monkeypatch.setattr(metrics, "_ENABLED", False)

    metrics.record_anthropic_usage(
        tenant_id=uuid4(),
        intent_tier="simple",
        model_used="haiku",
        input_tokens=10,
        output_tokens=5,
    )
    metrics.record_cohere_rerank(tenant_id=uuid4())

    in_counter.add.assert_not_called()
    counter.add.assert_not_called()


def test_record_anthropic_usage_never_raises_on_counter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = MagicMock()
    boom.add.side_effect = RuntimeError("exporter down")
    monkeypatch.setattr(metrics, "_anthropic_input_tokens", boom)
    monkeypatch.setattr(metrics, "_ENABLED", True)

    # Telemetry failure must not bubble into the reply path.
    metrics.record_anthropic_usage(
        tenant_id=uuid4(),
        intent_tier="simple",
        model_used="haiku",
        input_tokens=10,
        output_tokens=0,
    )


# ---------------------------------------------------------------------------
# Integration: generate_response wires the recorder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_response_records_token_usage(
    mock_anthropic: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="ok",
        input_tokens=900,
        output_tokens=120,
        cache_creation_input_tokens=400,
        cache_read_input_tokens=1500,
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        llm.metrics,
        "record_anthropic_usage",
        lambda **kw: calls.append(kw),
    )

    tid = uuid4()
    await llm.generate_response(
        tenant_id=tid,
        rag_context="",
        conversation_history=[],
        user_message="what time you open",
        intent="simple",
    )

    assert len(calls) == 1
    rec = calls[0]
    assert rec["tenant_id"] == tid
    assert rec["intent_tier"] == "simple"
    assert rec["model_used"] == "haiku"
    assert rec["input_tokens"] == 900
    assert rec["output_tokens"] == 120
    # Prompt-cache tokens flow through from the envelope's UsageMetrics.
    assert rec["cache_write_tokens"] == 400
    assert rec["cache_read_tokens"] == 1500


@pytest.mark.asyncio
async def test_classify_intent_records_usage_when_tenant_id_given(
    mock_anthropic: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.services import llm

    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="medium",
        input_tokens=40,
        output_tokens=1,
        cache_read_input_tokens=1200,
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        llm.metrics, "record_anthropic_usage", lambda **kw: calls.append(kw)
    )

    tid = uuid4()
    await llm.classify_intent("what's the price", tenant_id=tid)

    assert len(calls) == 1
    rec = calls[0]
    assert rec["tenant_id"] == tid
    assert rec["intent_tier"] == "classify"
    assert rec["model_used"] == "haiku"
    assert rec["input_tokens"] == 40
    assert rec["cache_read_tokens"] == 1200


@pytest.mark.asyncio
async def test_classify_intent_emits_nothing_without_tenant_id(
    mock_anthropic: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.services import llm

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        llm.metrics, "record_anthropic_usage", lambda **kw: calls.append(kw)
    )

    await llm.classify_intent("hello")  # no tenant_id → no telemetry

    assert calls == []


@pytest.mark.asyncio
async def test_embed_query_records_tokens_when_tenant_id_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.services import embed

    embed._reset_client_for_testing()
    resp = MagicMock()
    item = MagicMock()
    item.embedding = [0.5] * 1024
    resp.data = [item]
    resp.usage = MagicMock(total_tokens=37, prompt_tokens=37)
    client = MagicMock()
    client.embeddings.create = AsyncMock(return_value=resp)
    monkeypatch.setattr(embed, "_get_embed_client", lambda: client)

    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(
        embed.metrics, "record_openrouter_embedding", lambda **kw: recorded.append(kw)
    )

    tid = uuid4()
    await embed.embed_query("what time you open", tenant_id=tid)

    assert recorded == [{"tenant_id": tid, "tokens": 37}]


@pytest.mark.asyncio
async def test_embed_query_emits_nothing_without_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.services import embed

    embed._reset_client_for_testing()
    resp = MagicMock()
    item = MagicMock()
    item.embedding = [0.5] * 1024
    resp.data = [item]
    resp.usage = MagicMock(total_tokens=37)
    client = MagicMock()
    client.embeddings.create = AsyncMock(return_value=resp)
    monkeypatch.setattr(embed, "_get_embed_client", lambda: client)

    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(
        embed.metrics, "record_openrouter_embedding", lambda **kw: recorded.append(kw)
    )

    await embed.embed_query("hello")  # no tenant_id → no telemetry

    assert recorded == []


@pytest.mark.asyncio
async def test_generate_response_stub_emits_no_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.services import llm

    monkeypatch.setattr(llm, "get_settings", lambda: _StubSettings())
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        llm.metrics, "record_anthropic_usage", lambda **kw: calls.append(kw)
    )

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="",
        conversation_history=[],
        user_message="hello",
        intent="simple",
    )

    assert calls == []


class _StubSettings:
    """Minimal settings double with stub mode on (early-return path)."""

    llm_stub_enabled = True
