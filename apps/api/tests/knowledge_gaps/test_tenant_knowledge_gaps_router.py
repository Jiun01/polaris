"""Tenant Knowledge Gaps router tests — Knowledge Gap Detection / M3.

Mock ``get_tenant_context`` + ``get_request_conn`` so no DB is needed. Covers:
  * Ultra tenant → 200 with ranked themes (aggregate query mocked).
  * Starter tenant → 403 KNOWLEDGE_GAPS_TIER_BLOCKED, no query run.
  * Ultra tenant with no misses → 200 empty-state.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET", "pytest-secret-32-bytes-padded-xxxxxxxxxx")
os.environ.setdefault("JWT_ISSUER", "polaris-portal")


from polaris_api.deps.tenant import (  # noqa: E402
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.routers.tenant_knowledge_gaps import (  # noqa: E402
    router as kg_router,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")

_TENANT_ID = uuid4()
_USER_ID = uuid4()


def _ctx(plan_tier: str) -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT_ID,
        user_id=_USER_ID,
        role="admin",
        plan_tier=plan_tier,
        status="active",
    )


def _conn(total: int, rows: list[dict[str, object]]) -> AsyncMock:
    conn = AsyncMock(spec=asyncpg.Connection)
    conn.fetchval = AsyncMock(return_value=total)
    conn.fetch = AsyncMock(return_value=rows)
    return conn


def _app(ctx: TenantContext, conn: AsyncMock) -> FastAPI:
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/tenant/knowledge-gaps")
    app.dependency_overrides[get_tenant_context] = lambda: ctx

    async def _conn_override() -> AsyncIterator[AsyncMock]:
        yield conn

    app.dependency_overrides[get_request_conn] = _conn_override
    return app


async def test_ultra_gets_ranked_themes() -> None:
    rows = [
        {
            "norm_q": "can i get a refund",
            "example_question": "can I get a refund?",
            "ask_count": 12,
            "recent_count": 5,
            "last_seen": datetime(2026, 6, 6, tzinfo=UTC),
            "handoff_count": 2,
            "no_retrieval_count": 9,
            "low_retrieval_count": 1,
        }
    ]
    app = _app(_ctx("ultra"), _conn(20, rows))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/tenant/knowledge-gaps/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_tier"] == "ultra"
    assert body["window_days"] == 30
    assert body["total_misses"] == 20
    assert len(body["themes"]) == 1
    theme = body["themes"][0]
    assert theme["norm_q"] == "can i get a refund"
    assert theme["example_question"] == "can I get a refund?"
    assert theme["ask_count"] == 12
    assert theme["no_retrieval_count"] == 9


async def test_starter_is_blocked_403() -> None:
    conn = _conn(0, [])
    app = _app(_ctx("starter"), conn)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/tenant/knowledge-gaps/summary")

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "KNOWLEDGE_GAPS_TIER_BLOCKED"
    conn.fetch.assert_not_called()  # gate short-circuits before any query


async def test_ultra_empty_state() -> None:
    app = _app(_ctx("enterprise"), _conn(0, []))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/tenant/knowledge-gaps/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_misses"] == 0
    assert body["themes"] == []
