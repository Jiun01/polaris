"""Unit tests for services/knowledge_search.py.

Exercises both wrapper paths (hybrid + vector-only) against an
AsyncMock asyncpg.Connection. Real-DB RPC behaviour — RLS gates,
branch scoping, RRF math — is covered by the integration test in
``test_search_rpcs_rls.py``; this file proves the Python wrapper
shapes returned rows into ``KnowledgeChunkHit`` correctly and that
``search_hybrid`` degrades to vector-only on parser failures.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from polaris_api.services.knowledge_search import (
    search_hybrid,
    search_vector_only,
)


@pytest.fixture
def mock_conn() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# search_hybrid
# ---------------------------------------------------------------------------


async def test_hybrid_returns_hits_with_rrf_fields(mock_conn: AsyncMock) -> None:
    chunk_id = uuid4()
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": chunk_id,
                "content": "warranty covers 12 months",
                "source_name": "policy.pdf",
                "rrf_score": 0.0327,
                "vector_rank": 1,
                "bm25_rank": 2,
                "vector_distance": 0.21,
            }
        ]
    )
    hits = await search_hybrid(
        mock_conn, uuid4(), "warranty", [0.1] * 1024, top_k=5
    )
    assert len(hits) == 1
    assert hits[0].id == chunk_id
    assert hits[0].rrf_score == pytest.approx(0.0327, rel=1e-3)
    assert hits[0].vector_rank == 1
    assert hits[0].bm25_rank == 2
    # F2 (migration 113): cosine distance is threaded onto the hit.
    assert hits[0].distance == pytest.approx(0.21, rel=1e-3)


async def test_hybrid_falls_back_to_vector_on_syntax_error(
    mock_conn: AsyncMock,
) -> None:
    """websearch_to_tsquery parse failure → vector-only fallback."""
    chunk_id = uuid4()

    async def _fetch(sql: str, *args: object) -> list[dict[str, object]]:
        # First call (hybrid RPC) raises; second call (vector RPC) returns.
        if "match_knowledge_chunks_hybrid" in sql:
            raise asyncpg.PostgresError("syntax error in tsquery")
        return [
            {
                "id": chunk_id,
                "content": "fallback hit",
                "source_name": "doc.txt",
                "distance": 0.3,
            }
        ]

    mock_conn.fetch = AsyncMock(side_effect=_fetch)
    hits = await search_hybrid(
        mock_conn, uuid4(), "-leading-dash", [0.0] * 1024
    )
    assert len(hits) == 1
    assert hits[0].id == chunk_id
    # Synthesised rrf_score = 1 - distance for parity with hybrid path.
    assert hits[0].rrf_score == pytest.approx(0.7, rel=1e-3)
    assert hits[0].distance == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# search_vector_only
# ---------------------------------------------------------------------------


async def test_vector_only_returns_distance_and_synthesised_score(
    mock_conn: AsyncMock,
) -> None:
    chunk_id = uuid4()
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": chunk_id,
                "content": "emoji-friendly content",
                "source_name": "tweet.md",
                "distance": 0.1,
            }
        ]
    )
    hits = await search_vector_only(mock_conn, uuid4(), [0.0] * 1024)
    assert len(hits) == 1
    assert hits[0].distance == pytest.approx(0.1)
    assert hits[0].rrf_score == pytest.approx(0.9, rel=1e-3)
    assert hits[0].vector_rank is None
    assert hits[0].bm25_rank is None


async def test_branch_scoped_filter_passed_through(
    mock_conn: AsyncMock,
) -> None:
    """tenant_branch_id arg reaches the SQL call positionally."""
    branch_id = uuid4()
    mock_conn.fetch = AsyncMock(return_value=[])
    await search_vector_only(
        mock_conn, uuid4(), [0.0] * 1024, tenant_branch_id=branch_id, top_k=3
    )
    mock_conn.fetch.assert_awaited_once()
    # Positional args: (sql, embedding, tenant_id, branch_id, top_k).
    args = mock_conn.fetch.await_args.args
    assert args[3] == branch_id
    assert args[4] == 3
