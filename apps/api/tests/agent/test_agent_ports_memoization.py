"""Per-turn embed/search memoization in agent ports (audit Batch B / B3).

Verifies RAGAdapter + ProductsAdapter share one embed-query memo dict per
turn (as built by ``build_ports``), and that RAGAdapter additionally
memoizes ``search_hybrid`` results per ``(query, top_k)`` so a repeated
``search_knowledge`` tool call skips embed + hybrid SQL + Cohere rerank
entirely.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.agent.ports import ProductsAdapter, RAGAdapter

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def test_shared_embed_cache_called_once_across_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAGAdapter.search then ProductsAdapter.search_products with the SAME
    query string, sharing one embed_cache dict — embed_query fires ONCE."""
    from polaris_api.services.agent import ports as ports_mod

    embed_mock = AsyncMock(return_value=[0.1, 0.2, 0.3])
    monkeypatch.setattr(ports_mod.embed, "embed_query", embed_mock)
    monkeypatch.setattr(
        ports_mod.rag, "search_hybrid", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        ports_mod.products_search,
        "search_products_hybrid",
        AsyncMock(return_value=[]),
    )

    conn: Any = object()
    tenant_id = uuid4()
    embed_cache: dict[str, list[float]] = {}

    rag_adapter = RAGAdapter(conn=conn, redis=None, embed_cache=embed_cache)
    products_adapter = ProductsAdapter(conn=conn, embed_cache=embed_cache)

    await rag_adapter.search(query="q", tenant_id=tenant_id, top_k=5)
    await products_adapter.search_products(query="q", tenant_id=tenant_id, top_k=5)

    assert embed_mock.await_count == 1


async def test_rag_adapter_memoizes_search_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated search_knowledge call with the same (query, top_k) reuses
    the cached hits — no second embed call, no second hybrid SQL."""
    from polaris_api.services.agent import ports as ports_mod

    embed_mock = AsyncMock(return_value=[0.1, 0.2, 0.3])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(ports_mod.embed, "embed_query", embed_mock)
    monkeypatch.setattr(ports_mod.rag, "search_hybrid", search_mock)

    conn: Any = object()
    tenant_id = uuid4()
    adapter = RAGAdapter(conn=conn, redis=None, embed_cache={})

    await adapter.search(query="what time do you open", tenant_id=tenant_id, top_k=5)
    await adapter.search(query="what time do you open", tenant_id=tenant_id, top_k=5)

    assert embed_mock.await_count == 1
    assert search_mock.await_count == 1


async def test_build_ports_shares_one_embed_cache_between_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_ports wires RAGAdapter + ProductsAdapter to the SAME embed_cache
    instance, not two independent dicts."""
    from polaris_api.services.agent import ports as ports_mod
    from polaris_api.services.chatwoot import client as cw_client_mod

    monkeypatch.setattr(
        cw_client_mod, "get_chatwoot_client", lambda: object()
    )

    conn: Any = object()
    redis: Any = object()
    meta_client: Any = object()

    built = await ports_mod.build_ports(conn=conn, redis=redis, meta_client=meta_client)

    rag_adapter = built._rag
    products_adapter = built._products
    assert isinstance(rag_adapter, RAGAdapter)
    assert isinstance(products_adapter, ProductsAdapter)
    assert rag_adapter._embed_cache is products_adapter._embed_cache
