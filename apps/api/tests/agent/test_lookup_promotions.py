"""WS2 — lookup_promotions tool + branch-scoped product/promo adapters.

Covers the new agent promo tool and that ProductsAdapter/PromotionsAdapter thread
the conversation's ``tenant_branch_id`` into the underlying search/lookup (the
branch-scoping gap the audit flagged).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from polaris_api.services.agent import ports as ports_mod
from polaris_api.services.agent.ports import Ports, ProductsAdapter, PromotionsAdapter
from polaris_api.services.agent.tools.base import ToolContext
from polaris_api.services.agent.tools.lookup_promotions import lookup_promotions
from polaris_api.services.products import search as products_search
from polaris_api.services.promotions import lookup as promotions_lookup
from polaris_api.services.promotions.lookup import ActivePromotion

pytestmark = pytest.mark.asyncio(loop_scope="function")

_TENANT = uuid4()
_BRANCH = uuid4()


class _FakePromotionsPort:
    def __init__(self, promos: list[ActivePromotion]) -> None:
        self._promos = promos
        self.called_with: Any = None

    async def fetch_active(self, *, tenant_id: Any) -> list[ActivePromotion]:
        self.called_with = tenant_id
        return self._promos


def _ctx(port: _FakePromotionsPort) -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        conversation_id=None,
        channel="whatsapp",
        channel_user_id="+60123",
        customer_name=None,
        last_user_message="any deals?",
        conn=None,  # not used by lookup_promotions
        ports=Ports(promotions=port),
    )


async def test_lookup_promotions_formats_active() -> None:
    promos = [
        ActivePromotion(
            id=uuid4(),
            title="Set Lunch",
            description="All set lunches",
            discount_text="20% OFF",
            terms="Mon-Fri",
        )
    ]
    port = _FakePromotionsPort(promos)
    res = await lookup_promotions({}, _ctx(port))
    assert not res.is_error
    assert "Set Lunch" in res.content and "20% OFF" in res.content and "Mon-Fri" in res.content
    assert port.called_with == _TENANT


async def test_lookup_promotions_empty_says_none() -> None:
    res = await lookup_promotions({}, _ctx(_FakePromotionsPort([])))
    assert "no active promotions" in res.content.lower()


async def test_promotions_adapter_threads_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_fetch(conn: Any, tenant_id: Any, *, tenant_branch_id: Any = None) -> list[Any]:
        captured["tenant_branch_id"] = tenant_branch_id
        return []

    monkeypatch.setattr(promotions_lookup, "fetch_active_promotions", fake_fetch)
    adapter = PromotionsAdapter(conn=None, tenant_branch_id=_BRANCH)
    await adapter.fetch_active(tenant_id=_TENANT)
    assert captured["tenant_branch_id"] == _BRANCH


async def test_products_adapter_threads_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_hybrid(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    async def fake_embed(query: str, tenant_id: Any, cache: Any) -> list[float]:
        return [0.0]

    monkeypatch.setattr(products_search, "search_products_hybrid", fake_hybrid)
    monkeypatch.setattr(ports_mod, "_embed_cached", fake_embed)
    adapter = ProductsAdapter(conn=None, tenant_branch_id=_BRANCH)
    await adapter.search_products(query="x", tenant_id=_TENANT)
    assert captured["tenant_branch_id"] == _BRANCH
