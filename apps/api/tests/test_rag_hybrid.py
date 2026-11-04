"""Hybrid search wrapper tests — Phase 4 PR-2.

Covers ``services/rag.py::search_hybrid`` happy-path delegation to
``services/knowledge_search.py`` plus the per-tenant Redis tier cache.
Tier-gate + Cohere rerank logic live in test_rag_cohere_tier_gate.py;
empty-result fallback in test_rag_vector_fallback.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.conftest import make_mock_chunk

pytestmark = pytest.mark.asyncio


def _make_conn(
    *,
    tier_row: dict | None,
    has_chunks: bool = True,
) -> AsyncMock:
    """Stub asyncpg.Connection.

    ``tier_row`` is what `SELECT plan_tier, cohere_rerank_enabled FROM
    tenants WHERE id = $1` returns (None = no row found).
    ``has_chunks`` drives the EXISTS check for the empty-result
    fallback path.
    """
    conn = AsyncMock()

    async def fetchrow(sql: str, *args, **kwargs):
        if "FROM tenants" in sql:
            return tier_row
        if "FROM knowledge_chunks" in sql:
            return {"has": has_chunks}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


async def test_starter_no_override_calls_search_hybrid_top_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starter + no override → 1 hybrid call with top_k=5, no Cohere."""
    from polaris_api.services import knowledge_search, rag

    chunk = make_mock_chunk(content="fact 1")
    mock_search = AsyncMock(return_value=[chunk])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    conn = _make_conn(
        tier_row={"plan_tier": "starter", "cohere_rerank_enabled": None},
    )
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="hello",
        query_embedding=[0.1] * 1024,
    )

    assert result == [chunk]
    mock_search.assert_called_once()
    # top_k stays at 5 (not the 25 used in rerank path).
    assert mock_search.call_args.kwargs["top_k"] == 5


async def test_pro_no_override_calls_search_hybrid_top_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pro tier without override stays on hybrid-5 path."""
    from polaris_api.services import knowledge_search, rag

    chunk = make_mock_chunk()
    mock_search = AsyncMock(return_value=[chunk])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    conn = _make_conn(
        tier_row={"plan_tier": "pro", "cohere_rerank_enabled": None},
    )
    result = await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="hi",
        query_embedding=[0.0] * 1024,
    )

    assert result == [chunk]
    assert mock_search.call_args.kwargs["top_k"] == 5


async def test_unknown_tenant_defaults_to_starter_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing tenant row → behaves like Starter (no rerank)."""
    from polaris_api.services import knowledge_search, rag

    mock_search = AsyncMock(return_value=[make_mock_chunk()])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    conn = _make_conn(tier_row=None)
    await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
    )

    assert mock_search.call_args.kwargs["top_k"] == 5


async def test_tenant_branch_id_threaded_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_branch_id forwarded to the underlying search call."""
    from polaris_api.services import knowledge_search, rag

    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    conn = _make_conn(
        tier_row={"plan_tier": "starter", "cohere_rerank_enabled": None},
        has_chunks=False,  # avoid fallback path
    )
    branch_id = uuid4()
    await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
        tenant_branch_id=branch_id,
    )

    assert mock_search.call_args.kwargs["tenant_branch_id"] == branch_id


async def test_top_k_override_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-provided top_k flows through (non-rerank path only)."""
    from polaris_api.services import knowledge_search, rag

    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    conn = _make_conn(
        tier_row={"plan_tier": "starter", "cohere_rerank_enabled": None},
        has_chunks=False,
    )
    await rag.search_hybrid(
        conn=conn,
        tenant_id=uuid4(),
        query_text="q",
        query_embedding=[0.0] * 1024,
        top_k=3,
    )

    assert mock_search.call_args.kwargs["top_k"] == 3


async def test_redis_caches_tier_info_across_calls(
    monkeypatch: pytest.MonkeyPatch, fake_redis,
) -> None:
    """Second call within TTL hits Redis, no second SELECT on tenants."""
    from polaris_api.services import knowledge_search, rag

    mock_search = AsyncMock(return_value=[make_mock_chunk()])
    monkeypatch.setattr(knowledge_search, "search_hybrid", mock_search)

    conn = _make_conn(
        tier_row={"plan_tier": "starter", "cohere_rerank_enabled": None},
    )
    tenant_id = uuid4()

    await rag.search_hybrid(
        conn=conn, tenant_id=tenant_id,
        query_text="q1", query_embedding=[0.0] * 1024, redis=fake_redis,
    )
    await rag.search_hybrid(
        conn=conn, tenant_id=tenant_id,
        query_text="q2", query_embedding=[0.0] * 1024, redis=fake_redis,
    )

    # fetchrow was called for tenant tier on the first call only;
    # second call hits Redis.
    tenants_calls = [
        c for c in conn.fetchrow.await_args_list
        if "FROM tenants" in c.args[0]
    ]
    assert len(tenants_calls) == 1


async def test_redis_cache_key_includes_tenant_id(
    monkeypatch: pytest.MonkeyPatch, fake_redis,
) -> None:
    """Tier cache key includes tenant_id so two tenants don't collide."""
    from polaris_api.services import knowledge_search, rag

    monkeypatch.setattr(
        knowledge_search, "search_hybrid", AsyncMock(return_value=[]),
    )

    conn = _make_conn(
        tier_row={"plan_tier": "starter", "cohere_rerank_enabled": None},
        has_chunks=False,
    )
    tenant_id = uuid4()
    await rag.search_hybrid(
        conn=conn, tenant_id=tenant_id,
        query_text="q", query_embedding=[0.0] * 1024, redis=fake_redis,
    )

    cached = await fake_redis.get(f"tier:{tenant_id}")
    assert cached is not None
    # Different tenant key should NOT collide.
    other_cached = await fake_redis.get(f"tier:{uuid4()}")
    assert other_cached is None
