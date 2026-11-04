"""WS2 — lookup_products tool honesty.

Pins the honest-empty behaviour (the tool must report an empty catalog plainly,
never fabricate) and the non-empty formatting. The observed live fabrication was
model behaviour nudged by the persona ``answering_rules``; the guardrail fix
lives in ``_AGENT_TOOL_RULES``. These tests guard the *tool* contract so the
honest empty-result string can't silently regress.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from polaris_api.services.agent.ports import Ports
from polaris_api.services.agent.tools.base import ToolContext
from polaris_api.services.agent.tools.lookup_products import lookup_products

pytestmark = pytest.mark.asyncio(loop_scope="function")

_TENANT = uuid4()


class _FakeProductsPort:
    def __init__(self, hits: list[Any]) -> None:
        self._hits = hits
        self.called_with: Any = None

    async def search_products(
        self, *, query: str, tenant_id: Any, top_k: int = 5
    ) -> list[Any]:
        self.called_with = (query, tenant_id, top_k)
        return self._hits


def _ctx(port: _FakeProductsPort) -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        conversation_id=None,
        channel="whatsapp",
        channel_user_id="+60123",
        customer_name=None,
        last_user_message="what do you have?",
        conn=None,  # not used by lookup_products
        ports=Ports(products=port),
    )


async def test_lookup_products_empty_is_honest() -> None:
    port = _FakeProductsPort([])
    res = await lookup_products({"query": "anything"}, _ctx(port))
    assert not res.is_error
    assert "no matching products" in res.content.lower()
    assert port.called_with == ("anything", _TENANT, 5)


async def test_lookup_products_formats_hits() -> None:
    hits = [SimpleNamespace(name="Widget", description="A small widget")]
    res = await lookup_products({"query": "widget"}, _ctx(_FakeProductsPort(hits)))
    assert not res.is_error
    assert "Widget" in res.content
    assert "A small widget" in res.content


async def test_lookup_products_blank_query_falls_back_to_last_message() -> None:
    port = _FakeProductsPort([])
    await lookup_products({"query": ""}, _ctx(port))
    # Blank query defaults to the customer's last message.
    assert port.called_with == ("what do you have?", _TENANT, 5)
