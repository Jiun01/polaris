"""Empty-result vector-fallback tests — Phase 4 PR-2.

When hybrid returns 0 rows but the tenant has knowledge_chunks, the
wrapper re-queries via vector-only. Per deep-dive §5.2: defends
against typo-heavy queries where BM25 returns nothing but the vector
path would have hit. Distinct from the SQL-layer PostgresError
fallback already inside ``knowledge_search.search_hybrid`` (which
catches parser failures, not legitimate empty results).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.conftest import make_mock_chunk

pytestmark = pytest.mark.asyncio


def _make_conn(*, has_chunks: bool) -> AsyncMock:
    conn = AsyncMock()

    async def fetchrow(sql: str, *args, **kwargs):
        if "FROM tenants" in sql:
            return {"plan_tier": "starter", "cohere_rerank_enabled": None}
        if "FROM knowledge_chunks" in sql:
            return {"has": has_chunks}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


async def test_empty_hybrid_with_chunks_falls_back_to_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hybrid returns [] + tenant has chunks → search_vector_only called."""
    from polaris_api.services import knowledge_search, rag

    vector_hit = make_mock_chunk(content="vector recovered")
    mock_hybrid = AsyncMock(return_value=[])
    mock_vector = AsyncMock(return_value=[vector_hit])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_hybrid)
    monkeypatch.setattr(knowledge_search, "search_vector_only", mock_vector)

    conn = _make_conn(has_chunks=True)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="typo-heavy qry",
        query_embedding=[0.1] * 1024,
    )

    assert result == [vector_hit]
    mock_hybrid.assert_awaited_once()
    mock_vector.assert_awaited_once()
    # Same top_k passed through.
    assert mock_vector.call_args.kwargs["top_k"] == 5


async def test_empty_hybrid_no_chunks_returns_empty_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant with no KB → empty list returned, no vector_only call."""
    from polaris_api.services import knowledge_search, rag

    mock_hybrid = AsyncMock(return_value=[])
    mock_vector = AsyncMock(return_value=[])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_hybrid)
    monkeypatch.setattr(knowledge_search, "search_vector_only", mock_vector)

    conn = _make_conn(has_chunks=False)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="any",
        query_embedding=[0.0] * 1024,
    )

    assert result == []
    mock_vector.assert_not_called()


async def test_nonempty_hybrid_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: hybrid returns hits → no fallback path traversed."""
    from polaris_api.services import knowledge_search, rag

    hit = make_mock_chunk()
    mock_hybrid = AsyncMock(return_value=[hit])
    mock_vector = AsyncMock(return_value=[])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_hybrid)
    monkeypatch.setattr(knowledge_search, "search_vector_only", mock_vector)

    conn = _make_conn(has_chunks=True)
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    assert result == [hit]
    mock_vector.assert_not_called()


async def test_fallback_preserves_tenant_branch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch scoping carried into the vector-only fallback call."""
    from polaris_api.services import knowledge_search, rag

    mock_hybrid = AsyncMock(return_value=[])
    mock_vector = AsyncMock(return_value=[])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_hybrid)
    monkeypatch.setattr(knowledge_search, "search_vector_only", mock_vector)

    branch_id = uuid4()
    conn = _make_conn(has_chunks=True)
    await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
        tenant_branch_id=branch_id,
    )

    assert mock_vector.call_args.kwargs["tenant_branch_id"] == branch_id


async def test_rerank_empty_falls_back_to_unranked_hybrid_not_vector(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """Ultra-tier rerank path: when no chunk clears the threshold, serve the
    un-reranked hybrid candidates — NOT a fresh vector-only search (API-09).

    Rerank semantics differ from the BM25 empty-result case: an empty Cohere
    response means "nothing cleared 0.3", not "BM25 was wrong". The fix returns
    the already-fetched hybrid top_k (better than a context-free reply) while
    deliberately NOT calling ``search_vector_only`` — that would inject results
    the hybrid stage didn't surface. See test_rag_rerank_empty_fallback.py.
    """
    from polaris_api.services import knowledge_search, rag

    candidates = [make_mock_chunk()]
    mock_hybrid = AsyncMock(return_value=candidates)
    mock_vector = AsyncMock(return_value=[])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_hybrid)
    monkeypatch.setattr(knowledge_search, "search_vector_only", mock_vector)
    # Cohere returns 0 results above threshold.
    from tests.conftest import make_mock_cohere_rerank_response
    mock_cohere.rerank.return_value = make_mock_cohere_rerank_response(
        scores=[0.1],
    )

    conn = AsyncMock()
    async def fetchrow(sql: str, *args, **kwargs):
        if "FROM tenants" in sql:
            return {"plan_tier": "ultra", "cohere_rerank_enabled": None}
        if "FROM knowledge_chunks" in sql:
            return {"has": True}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")
    conn.fetchrow = AsyncMock(side_effect=fetchrow)

    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    # API-09: fall back to the un-reranked hybrid candidates, not [].
    assert result == candidates
    # ...but never via a separate vector-only search on the rerank path.
    mock_vector.assert_not_called()
