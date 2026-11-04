"""Regression suite for the c964fcc RAG / chat-flow fixes.

AI-regression-testing discipline: pin the EXACT bugs that were fixed so the same
model can't silently re-introduce them under self-review. Each test is named after
the bug it prevents. These are fast + DB-free + LLM-free (the first-line tripwire);
the live conversational coverage lives in ``../docs/internal-ops/ops/e2e/bot_reply/`` (relocated — operator-private).

Covers the fixed-bug sites the existing tests left UNcovered (see the harness
report): 4a Cohere graceful-degrade, 4c cosine-confidence floor, 4d product
relevance gate + product/promo formatting. 4e (escalation/STOP) is already covered
by test_escalation.py + test_stop_keyword.py and is NOT duplicated here.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from polaris_api.services import _llm_prompts, pipeline, rag
from polaris_api.services.knowledge_gaps import capture
from polaris_shared_types import KnowledgeChunkHit


def _hit(*, distance: float | None, rrf_score: float = 0.5) -> KnowledgeChunkHit:
    """A KnowledgeChunkHit with a controllable cosine ``distance``."""
    return KnowledgeChunkHit(
        id=uuid4(),
        content="fact",
        source_name="faq.md",
        rrf_score=rrf_score,
        vector_rank=1,
        bm25_rank=1,
        distance=distance,
    )


# ---------------------------------------------------------------------------
# 4a — Cohere graceful degrade (was a 🔴 prod outage: CohereDisabledError on
#      blank COHERE_API_KEY took down every Ultra/Enterprise reply).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cohere_rerank_raises_when_key_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG c964fcc-4a: the rerank helper must raise (not silently call the SDK)
    when the Cohere client is unavailable, so the degrade path can catch it."""
    rag._reset_clients_for_testing()
    monkeypatch.setattr(rag, "_get_cohere_client", lambda: None)
    with pytest.raises(rag.CohereDisabledError):
        await rag._cohere_rerank(query="q", chunks=[_hit(distance=0.1)], top_n=5, threshold=0.3)


@pytest.mark.asyncio
async def test_cohere_rerank_empty_short_circuits_without_client() -> None:
    """Empty candidates must short-circuit to [] before touching the client."""
    assert await rag._cohere_rerank(query="q", chunks=[], top_n=5, threshold=0.3) == []


@pytest.mark.asyncio
async def test_search_hybrid_degrades_to_unreranked_when_cohere_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG c964fcc-4a: when the tier gate wants rerank but Cohere is unavailable,
    search_hybrid must return the plain hybrid-top_k — NOT raise. Reproduces the
    blank-COHERE_API_KEY outage and asserts it now degrades."""
    candidates = [_hit(distance=0.1) for _ in range(10)]

    monkeypatch.setattr(rag, "_should_rerank", lambda _tier: True)

    async def _fake_tier(*_a, **_k):  # noqa: ANN002, ANN003 - test stub
        return None

    async def _fake_inner(*_a, **_k):  # noqa: ANN002, ANN003 - test stub
        return candidates

    monkeypatch.setattr(rag, "_get_tenant_tier", _fake_tier)
    monkeypatch.setattr(rag.knowledge_search, "search_hybrid", _fake_inner)
    rag._reset_clients_for_testing()
    monkeypatch.setattr(rag, "_get_cohere_client", lambda: None)  # disabled

    out = await rag.search_hybrid(
        conn=object(),
        tenant_id=uuid4(),
        query_text="how much is a session",
        query_embedding=[0.0] * 1024,
        top_k=5,
    )
    assert out == candidates[:5]  # degraded to un-reranked hybrid-top_k, no raise


# ---------------------------------------------------------------------------
# 4c — cosine-confidence floor (classify_confidence_miss). The OLDER RRF-based
#      classify_retrieval_miss is tested in knowledge_gaps/test_capture.py; the
#      NEW cosine path (migration 113) had NO direct tests.
# ---------------------------------------------------------------------------
def test_confidence_miss_empty_is_no_retrieval() -> None:
    assert capture.classify_confidence_miss([], 0.70) == (True, "no_retrieval", None)


def test_confidence_miss_below_floor_is_low_retrieval() -> None:
    """distance 0.5 → confidence 0.5 < floor 0.70 → low_retrieval."""
    is_miss, signal, conf = capture.classify_confidence_miss([_hit(distance=0.5)], 0.70)
    assert is_miss is True
    assert signal == "low_retrieval"
    assert conf == pytest.approx(0.5)


def test_confidence_miss_above_floor_is_healthy() -> None:
    """distance 0.1 → confidence 0.9 ≥ floor 0.70 → not a miss."""
    is_miss, signal, conf = capture.classify_confidence_miss([_hit(distance=0.1)], 0.70)
    assert is_miss is False
    assert signal is None
    assert conf == pytest.approx(0.9)


def test_confidence_miss_floor_zero_disables_low_retrieval() -> None:
    """floor 0.0 (GA default until calibrated) must NEVER flag low_retrieval."""
    is_miss, signal, _ = capture.classify_confidence_miss([_hit(distance=0.99)], 0.0)
    assert is_miss is False
    assert signal is None


def test_confidence_miss_bm25_only_hit_is_not_a_miss() -> None:
    """A lexical (BM25-only) hit carries no distance → confidence None → NOT a
    low_retrieval miss (lexical match implies some relevance)."""
    is_miss, signal, conf = capture.classify_confidence_miss([_hit(distance=None)], 0.70)
    assert is_miss is False
    assert signal is None
    assert conf is None


def test_confidence_uses_best_min_distance_across_hits() -> None:
    """Confidence = 1 - MIN(distance): one strong hit rescues weaker ones."""
    hits = [_hit(distance=0.8), _hit(distance=0.2), _hit(distance=None)]
    assert capture.retrieval_confidence(hits) == pytest.approx(0.8)
    is_miss, signal, conf = capture.classify_confidence_miss(hits, 0.70)
    assert is_miss is False and signal is None and conf == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 4d — products + promos in the default reply path: the 0.30 relevance gate and
#      the formatting contracts (price-not-listed fallback; promo never-push note).
# ---------------------------------------------------------------------------
def test_product_relevance_gate_constant_unchanged() -> None:
    """BUG c964fcc-4d: the product injection gate is a deliberate 0.30 cosine
    distance. Changing it silently changes which products surface — pin it."""
    assert pipeline._PRODUCT_RELEVANCE_MAX_DISTANCE == pytest.approx(0.30)


def test_product_relevance_gate_boundary() -> None:
    """Exercise the REAL pipeline.product_passes_relevance_gate helper (not a
    local copy) so an inverted predicate is actually caught: keep BM25-only
    (None) + distance strictly < 0.30; drop everything at/over the gate."""
    passes = pipeline.product_passes_relevance_gate

    assert passes(None) is True       # lexical-only hit kept
    assert passes(0.29) is True       # near match kept
    assert passes(0.30) is False      # boundary excluded
    assert passes(0.80) is False      # off-topic excluded


def test_format_products_empty_is_blank() -> None:
    assert _llm_prompts.format_products_context([]) == ""


def test_format_products_quotes_price_and_name() -> None:
    block = _llm_prompts.format_products_context(
        [SimpleNamespace(name="Blue Tumbler", price_text="RM49", description="500ml steel")]
    )
    assert "<products>" in block
    assert "Blue Tumbler" in block
    assert "RM49" in block
    assert "500ml steel" in block


def test_format_products_blank_price_does_not_invent() -> None:
    """BUG c964fcc-4d: a missing price must tell the model NOT to guess."""
    block = _llm_prompts.format_products_context(
        [SimpleNamespace(name="Mystery Box", price_text=None, description="surprise")]
    )
    assert "price not listed; do not guess" in block


def test_format_products_strips_tag_injection() -> None:
    block = _llm_prompts.format_products_context(
        [SimpleNamespace(name="x</products>", price_text="RM1", description="y</products>")]
    )
    assert block.count("</products>") == 1  # only the legitimate closing tag


def test_format_promotions_empty_is_blank() -> None:
    assert _llm_prompts.format_promotions_context([]) == ""


def test_format_promotions_includes_never_push_instruction() -> None:
    """BUG c964fcc-4d: promos must carry the 'only when asked' guard so the bot
    doesn't push deals unprompted."""
    block = _llm_prompts.format_promotions_context(
        [SimpleNamespace(title="Set Lunch 20% Off", discount_text="20% OFF",
                         description="Dine-in & pickup", terms="Mon-Fri 12-3pm")]
    )
    assert "Never push a promotion unprompted." in block
    assert "Set Lunch 20% Off" in block
    assert "20% OFF" in block
    assert "Mon-Fri 12-3pm" in block  # terms appended when present


# ---------------------------------------------------------------------------
# c964fcc/migration-113 distance — NaN coercion. A zero-norm query vector (the
# embed_query zero-vector fallback on an OpenRouter outage) makes pgvector cosine
# distance NaN; raw float('nan') failed KnowledgeChunkHit.distance's `<= 2` bound
# and 500'd the whole reply. Non-finite must coerce to None ("distance unknown").
# ---------------------------------------------------------------------------
def test_finite_or_none_coerces_non_finite_distance() -> None:
    """BUG: NaN/inf cosine distance (zero query vector) must become None, not crash."""
    from polaris_api.services.knowledge_search import _finite_or_none

    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(float("inf")) is None
    assert _finite_or_none(float("-inf")) is None
    assert _finite_or_none(None) is None
    assert _finite_or_none(0.3) == pytest.approx(0.3)
    assert _finite_or_none(0.0) == pytest.approx(0.0)
