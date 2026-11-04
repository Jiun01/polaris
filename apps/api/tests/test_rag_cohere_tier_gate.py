"""Cohere rerank tier-gate tests — Phase 4 PR-2.

The matrix tested below is the contract for ADR-026 Amendment 5th:

  | plan_tier   | cohere_rerank_enabled | rerank?                  |
  |-------------|-----------------------|--------------------------|
  | starter/pro | NULL or FALSE         | NO                       |
  | starter/pro | TRUE (override)       | YES (paid quality)       |
  | ultra/ent.  | NULL or TRUE          | YES                      |
  | ultra/ent.  | FALSE (override)      | NO (save cost)           |

Two further guarantees:
  - Placeholder COHERE_API_KEY in a rerank-required path raises
    CohereDisabledError (no silent leak with junk credentials).
  - When rerank is active, hybrid is fetched with top_k=25 (per
    ADR-032 Amendment 2026-05-15), then Cohere narrows to top_k with
    relevance_score ≥ 0.3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.conftest import (
    make_mock_chunk,
    make_mock_cohere_rerank_response,
)

pytestmark = pytest.mark.asyncio


def _make_conn(*, plan_tier: str, override: bool | None) -> AsyncMock:
    conn = AsyncMock()

    async def fetchrow(sql: str, *args, **kwargs):
        if "FROM tenants" in sql:
            return {"plan_tier": plan_tier, "cohere_rerank_enabled": override}
        if "FROM knowledge_chunks" in sql:
            return {"has": True}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


@pytest.mark.parametrize(
    "plan_tier,override,expect_rerank",
    [
        # Tier defaults (override=NULL)
        ("starter", None, False),
        ("pro", None, False),
        ("ultra", None, True),
        ("enterprise", None, True),
        # Override TRUE always reranks
        ("starter", True, True),
        ("pro", True, True),
        ("ultra", True, True),
        ("enterprise", True, True),
        # Override FALSE always skips rerank
        ("starter", False, False),
        ("pro", False, False),
        ("ultra", False, False),
        ("enterprise", False, False),
    ],
)
async def test_tier_gate_matrix(
    monkeypatch: pytest.MonkeyPatch,
    mock_cohere,
    plan_tier: str,
    override: bool | None,
    expect_rerank: bool,
) -> None:
    """All 12 cells of the (tier × override) matrix gate Cohere correctly."""
    from polaris_api.services import knowledge_search, rag

    chunk = make_mock_chunk()
    mock_search = AsyncMock(return_value=[chunk])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    # Align the Cohere mock to a single-candidate response so the
    # rerank path doesn't IndexError on result.index=1 with only one
    # source chunk.
    mock_cohere.rerank.return_value = make_mock_cohere_rerank_response(
        scores=[0.9],
    )

    conn = _make_conn(plan_tier=plan_tier, override=override)
    await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    if expect_rerank:
        mock_cohere.rerank.assert_awaited_once()
        # hybrid pre-fetched with top_k=25 (rerank pool).
        assert mock_search.call_args.kwargs["top_k"] == 25
    else:
        mock_cohere.rerank.assert_not_called()
        # hybrid pulled top_k=5 directly.
        assert mock_search.call_args.kwargs["top_k"] == 5


async def test_rerank_uses_multilingual_model_and_top_5(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """Rerank call shape: rerank-v3.5 model, top_n=5, return_documents=False."""
    from polaris_api.services import knowledge_search, rag

    chunks = [make_mock_chunk(content=f"chunk {i}") for i in range(25)]
    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=chunks),
    )
    mock_cohere.rerank.return_value = make_mock_cohere_rerank_response(
        scores=[0.9, 0.85, 0.7, 0.6, 0.4],
    )

    conn = _make_conn(plan_tier="ultra", override=None)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    rerank_kwargs = mock_cohere.rerank.call_args.kwargs
    assert rerank_kwargs["model"] == "rerank-v3.5"
    assert rerank_kwargs["top_n"] == 5
    assert rerank_kwargs["return_documents"] is False
    # 5 survivors (all >= 0.3 threshold).
    assert len(result) == 5


async def test_rerank_drops_below_threshold(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """Chunks with relevance_score < 0.3 are dropped from output."""
    from polaris_api.services import knowledge_search, rag

    chunks = [
        make_mock_chunk(content="strong"),    # 0.9 — kept
        make_mock_chunk(content="medium"),    # 0.5 — kept
        make_mock_chunk(content="weak"),      # 0.2 — DROPPED
        make_mock_chunk(content="trivial"),   # 0.05 — DROPPED
    ]
    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=chunks),
    )
    mock_cohere.rerank.return_value = make_mock_cohere_rerank_response(
        scores=[0.9, 0.5, 0.2, 0.05],
    )

    conn = _make_conn(plan_tier="ultra", override=None)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    assert [c.content for c in result] == ["strong", "medium"]


async def test_rerank_reorders_by_score(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """Cohere's ordering wins over the BM25/vector pre-ranking."""
    from polaris_api.services import knowledge_search, rag

    chunks = [
        make_mock_chunk(content="orig-0"),
        make_mock_chunk(content="orig-1"),
        make_mock_chunk(content="orig-2"),
    ]
    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=chunks),
    )
    # Cohere says: chunk at original-index 2 is best, then 0, then 1.
    response = make_mock_cohere_rerank_response(scores=[0.9, 0.8, 0.5])
    response.results[0].index = 2
    response.results[1].index = 0
    response.results[2].index = 1
    mock_cohere.rerank.return_value = response

    conn = _make_conn(plan_tier="ultra", override=None)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    assert [c.content for c in result] == ["orig-2", "orig-0", "orig-1"]


async def test_placeholder_cohere_key_degrades_when_rerank_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ultra tenant + placeholder key → DEGRADE to un-reranked hybrid (report F1).

    Previously this raised CohereDisabledError, which propagated uncaught out of
    pipeline.run() and dropped the entire reply for Ultra/Enterprise tenants when
    no Cohere key was provisioned (the prod default). search_hybrid now catches
    the guard and serves the plain hybrid-top_k result instead of crashing.
    """
    from polaris_api.config import get_settings
    from polaris_api.services import knowledge_search, rag

    monkeypatch.setenv("COHERE_API_KEY", "placeholder-cohere-key")
    get_settings.cache_clear()
    rag._reset_clients_for_testing()

    candidates = [make_mock_chunk(content="a"), make_mock_chunk(content="b")]
    monkeypatch.setattr(
        knowledge_search, "search_hybrid",
        AsyncMock(return_value=candidates),
    )

    conn = _make_conn(plan_tier="ultra", override=None)
    # Must NOT raise — degrades gracefully.
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )
    assert [c.content for c in result] == ["a", "b"]


async def test_starter_with_placeholder_cohere_key_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starter tenant w/ placeholder Cohere key is fine — no rerank attempted.

    This is the Phase 4 dogfood baseline state (per plan
    decision-lock 2: Cohere key stays placeholder until first Ultra+
    tenant signs up). Starter calls must not trip the guard.
    """
    from polaris_api.config import get_settings
    from polaris_api.services import knowledge_search, rag

    monkeypatch.setenv("COHERE_API_KEY", "placeholder-cohere-key")
    get_settings.cache_clear()
    rag._reset_clients_for_testing()

    chunk = make_mock_chunk()
    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=[chunk]),
    )

    conn = _make_conn(plan_tier="starter", override=None)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    assert result == [chunk]


async def test_empty_candidate_pool_skips_cohere_call(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """Ultra tenant + zero hybrid hits → no Cohere call (nothing to rerank)."""
    from polaris_api.services import knowledge_search, rag

    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=[]),
    )

    conn = _make_conn(plan_tier="ultra", override=None)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    assert result == []
    mock_cohere.rerank.assert_not_called()
