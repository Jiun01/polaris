"""Regression: Cohere rerank returning all-below-threshold must fall back.

Audit finding API-09. In ``rag.search_hybrid`` the rerank branch returned
``reranked`` directly. When every candidate scores below ``_RERANK_THRESHOLD``
(0.3), ``_cohere_rerank`` yields ``[]`` and the populated ``candidates`` were
discarded — an Ultra/Enterprise tenant with matching chunks got a context-free
reply (e.g. a colloquial-Malay question against an English KB ranks low).

The fix mirrors the CohereDisabledError degrade: when ``reranked`` is empty but
``candidates`` is not, serve the un-reranked hybrid top_k instead of nothing.

Mirrors test_rag_cohere_tier_gate.py for the conn/Cohere mocking shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.conftest import make_mock_chunk, make_mock_cohere_rerank_response

# Function-scoped loop so these async tests never share/close the session-scoped
# event loop that conftest's service_dsn holds (see conftest.py:21-37).
pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_ultra_conn() -> AsyncMock:
    """Ultra tier (→ rerank active) with a populated KB."""
    conn = AsyncMock()

    async def fetchrow(sql: str, *args, **kwargs):
        if "FROM tenants" in sql:
            return {"plan_tier": "ultra", "cohere_rerank_enabled": None}
        if "FROM knowledge_chunks" in sql:
            return {"has": True}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


async def test_all_candidates_below_threshold_falls_back_to_unranked(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """Every candidate < 0.3 → return the un-reranked hybrid top_k, not []."""
    from polaris_api.services import knowledge_search, rag

    candidates = [make_mock_chunk(content=f"chunk {i}") for i in range(3)]
    mock_search = AsyncMock(return_value=candidates)
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    # All scores below _RERANK_THRESHOLD (0.3) → survivors == [] in _cohere_rerank.
    mock_cohere.rerank.return_value = make_mock_cohere_rerank_response(
        scores=[0.10, 0.08, 0.05],
    )

    # top_k=2 with 3 candidates so the [:top_k] slice is actually exercised
    # (not a no-op as it would be when top_k >= len(candidates)).
    result = await rag.search_hybrid(
        conn=_make_ultra_conn(),
        tenant_id=uuid4(),
        query_text="korang ada stok tak",
        query_embedding=[0.0] * 1024,
        top_k=2,
    )

    # The bug returned []; the fix returns the un-reranked candidates, capped
    # at top_k.
    assert result == candidates[:2]
    assert len(result) == 2
    mock_cohere.rerank.assert_awaited_once()


async def test_some_candidates_above_threshold_still_reranks_normally(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """Fallback must NOT fire when at least one candidate survives rerank."""
    from polaris_api.services import knowledge_search, rag

    candidates = [make_mock_chunk(content=f"chunk {i}") for i in range(3)]
    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=candidates),
    )
    # First above threshold, rest below → one survivor (index 0).
    mock_cohere.rerank.return_value = make_mock_cohere_rerank_response(
        scores=[0.85, 0.10, 0.05],
    )

    result = await rag.search_hybrid(
        conn=_make_ultra_conn(),
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
        top_k=5,
    )

    # Only the surviving chunk is returned — not the full un-reranked set.
    assert result == [candidates[0]]


async def test_cohere_network_error_degrades_to_unranked(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """A real network/API failure (not CohereDisabledError) must degrade to
    the un-reranked hybrid top_k, same as the disabled-client case (silent-
    failure audit fix A2, 2026-07-02)."""
    import httpx
    from polaris_api.services import knowledge_search, rag

    candidates = [make_mock_chunk(content=f"chunk {i}") for i in range(3)]
    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=candidates),
    )
    mock_cohere.rerank.side_effect = httpx.ConnectError("connection refused")

    result = await rag.search_hybrid(
        conn=_make_ultra_conn(), tenant_id=uuid4(), query_text="q",
        query_embedding=[0.0] * 1024, top_k=2,
    )
    assert result == candidates[:2]


async def test_cohere_api_error_degrades_to_unranked(
    monkeypatch: pytest.MonkeyPatch, mock_cohere,
) -> None:
    """A Cohere SDK ApiError (non-2xx response) must degrade identically."""
    from cohere.core import ApiError
    from polaris_api.services import knowledge_search, rag

    candidates = [make_mock_chunk(content=f"chunk {i}") for i in range(3)]
    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=candidates),
    )
    mock_cohere.rerank.side_effect = ApiError(status_code=503, body="upstream error")

    result = await rag.search_hybrid(
        conn=_make_ultra_conn(), tenant_id=uuid4(), query_text="q",
        query_embedding=[0.0] * 1024, top_k=2,
    )
    assert result == candidates[:2]
