"""Unit tests for the pure RAG-eval scoring helpers — NO DB, NO network.

Exercises ``polaris_api.services.rag_eval`` with synthetic hit stubs so the
scoring math (rank / recall@k / MRR / per-lang summarize) is verified in
isolation from the real retrieval path. The runner script
(``apps/api/scripts/rag_eval.py``) is NOT exercised here — it needs a live DB +
embedding provider and is validated manually.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from polaris_api.services import rag_eval


@dataclass(slots=True)
class _Hit:
    """Minimal stand-in for ``KnowledgeChunkHit`` — only the two fields the
    scorer reads (``content`` + ``source_name``)."""

    content: str
    source_name: str = "doc.md"


# ---------------------------------------------------------------------------
# hit_rank
# ---------------------------------------------------------------------------


def test_hit_rank_found_at_second_position() -> None:
    hits = [
        _Hit(content="totally unrelated text"),
        _Hit(content="Yes, there is a free trial with 1,500 messages"),
        _Hit(content="more unrelated"),
    ]
    assert rag_eval.hit_rank(hits, ["free", "1,500"]) == 2


def test_hit_rank_first_match_wins() -> None:
    hits = [
        _Hit(content="our pricing starts at RM699"),
        _Hit(content="pricing also includes a free tier"),
    ]
    # Both contain "pricing"; the first satisfying hit is rank 1.
    assert rag_eval.hit_rank(hits, ["pricing"]) == 1


def test_hit_rank_miss_returns_none() -> None:
    hits = [
        _Hit(content="something about the weather"),
        _Hit(content="and something about lunch"),
    ]
    assert rag_eval.hit_rank(hits, ["free", "trial"]) is None


def test_hit_rank_is_case_insensitive() -> None:
    hits = [_Hit(content="The FREE Trial lasts 14 days")]
    assert rag_eval.hit_rank(hits, ["free trial"]) == 1


def test_hit_rank_empty_keywords_never_matches() -> None:
    hits = [_Hit(content="free trial available")]
    assert rag_eval.hit_rank(hits, []) is None


def test_hit_rank_empty_hits_returns_none() -> None:
    assert rag_eval.hit_rank([], ["free"]) is None


def test_hit_rank_with_page_match() -> None:
    hits = [
        _Hit(content="free trial info", source_name="/blog/news"),
        _Hit(content="free trial info", source_name="/pricing/"),
    ]
    # Keyword present in both, but only the /pricing/ source satisfies the page.
    assert rag_eval.hit_rank(hits, ["free"], expect_page="/pricing/") == 2


def test_hit_rank_page_mismatch_returns_none() -> None:
    hits = [_Hit(content="free trial", source_name="/blog/")]
    assert rag_eval.hit_rank(hits, ["free"], expect_page="/pricing/") is None


# ---------------------------------------------------------------------------
# recall_at_k
# ---------------------------------------------------------------------------


def test_recall_at_k_basic() -> None:
    # ranks [1, 3, None, 2] within k=5 → 3 of 4 recalled = 0.75
    assert rag_eval.recall_at_k([1, 3, None, 2], 5) == pytest.approx(0.75)


def test_recall_at_k_respects_k_cutoff() -> None:
    # rank 6 is outside top-5 → only rank 1 counts = 1 of 2 = 0.5
    assert rag_eval.recall_at_k([1, 6], 5) == pytest.approx(0.5)


def test_recall_at_k_empty_is_zero() -> None:
    assert rag_eval.recall_at_k([], 5) == 0.0


def test_recall_at_k_all_misses() -> None:
    assert rag_eval.recall_at_k([None, None], 5) == 0.0


# ---------------------------------------------------------------------------
# mrr
# ---------------------------------------------------------------------------


def test_mrr_basic() -> None:
    # (1/1 + 1/2 + 0) / 3
    assert rag_eval.mrr([1, 2, None]) == pytest.approx((1.0 + 0.5 + 0.0) / 3)


def test_mrr_all_top_rank() -> None:
    assert rag_eval.mrr([1, 1, 1]) == pytest.approx(1.0)


def test_mrr_empty_is_zero() -> None:
    assert rag_eval.mrr([]) == 0.0


def test_mrr_all_misses() -> None:
    assert rag_eval.mrr([None, None]) == 0.0


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_by_lang() -> None:
    rows = [
        {"q": "free trial?", "lang": "en", "rank": 1, "expect_any": ["free"]},
        {"q": "pricing?", "lang": "en", "rank": 2, "expect_any": ["price"]},
        {"q": "ada free trial?", "lang": "ms", "rank": None, "expect_any": ["percuma"]},
        {"q": "有免费试用吗?", "lang": "zh", "rank": 3, "expect_any": ["免费"]},
    ]
    summary = rag_eval.summarize(rows, k=5)

    assert summary["count"] == 4
    assert summary["k"] == 5
    # 3 of 4 recalled within k=5
    assert summary["recall_at_k"] == pytest.approx(0.75)
    # (1/1 + 1/2 + 0 + 1/3) / 4
    assert summary["mrr"] == pytest.approx((1.0 + 0.5 + 0.0 + (1.0 / 3)) / 4)

    assert summary["by_lang"]["en"]["count"] == 2
    assert summary["by_lang"]["en"]["recall_at_k"] == pytest.approx(1.0)
    assert summary["by_lang"]["ms"]["count"] == 1
    assert summary["by_lang"]["ms"]["recall_at_k"] == 0.0
    assert summary["by_lang"]["zh"]["count"] == 1
    assert summary["by_lang"]["zh"]["recall_at_k"] == pytest.approx(1.0)

    # exactly the one miss is reported as a failure
    assert len(summary["failures"]) == 1
    assert summary["failures"][0]["q"] == "ada free trial?"
    assert summary["failures"][0]["lang"] == "ms"


def test_summarize_empty_is_safe() -> None:
    summary = rag_eval.summarize([], k=5)
    assert summary["count"] == 0
    assert summary["recall_at_k"] == 0.0
    assert summary["mrr"] == 0.0
    assert summary["by_lang"] == {}
    assert summary["failures"] == []


def test_summarize_missing_lang_defaults_to_unknown() -> None:
    rows = [{"q": "x", "rank": 1, "expect_any": ["x"]}]
    summary = rag_eval.summarize(rows, k=5)
    assert "unknown" in summary["by_lang"]
    assert summary["by_lang"]["unknown"]["count"] == 1
