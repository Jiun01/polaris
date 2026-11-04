"""GET /api/tenant/knowledge/capacity — portal capacity-bar source.

Unit tests against a mocked asyncpg.Connection. The endpoint wraps the
same cap + live-SUM functions used by the intake gate, so the test
exercises wiring + clamping logic rather than re-proving the SQL math
(that's covered by test_knowledge_ingestion_service.py).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)

from polaris_api.deps.tenant import get_request_conn  # noqa: E402
from polaris_api.main import app  # noqa: E402

# Pin loop_scope=function — AsyncClient + dependency_overrides; see
# conftest.py docstring + the test_admin_chunking_config.py pin.
pytestmark = pytest.mark.asyncio(loop_scope="function")


def _mint(tenant_id: UUID) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": "admin",
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


def _active_tenant_row() -> dict[str, Any]:
    return {"status": "active", "plan_tier": "starter", "suspended_reason": None}


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_conn() -> AsyncIterator[tuple[AsyncClient, AsyncMock]]:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="SELECT 1")

    async def _override() -> AsyncIterator[AsyncMock]:
        yield conn

    app.dependency_overrides[get_request_conn] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, conn
    finally:
        app.dependency_overrides.pop(get_request_conn, None)


async def test_capacity_returns_used_cap_available(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _active_tenant_row(),
            {"effective_cap_mb": 100},
            {"used_mb": 30},
        ]
    )
    resp = await client.get(
        "/api/tenant/knowledge/capacity",
        headers={"Authorization": f"Bearer {_mint(tenant_id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"used_mb": 30, "cap_mb": 100, "available_mb": 70}


async def test_capacity_clamps_available_at_zero_when_over_cap(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """Enterprise override downgrade can leave used_mb > cap_mb."""
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _active_tenant_row(),
            {"effective_cap_mb": 100},
            {"used_mb": 250},
        ]
    )
    resp = await client.get(
        "/api/tenant/knowledge/capacity",
        headers={"Authorization": f"Bearer {_mint(tenant_id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["used_mb"] == 250
    assert body["cap_mb"] == 100
    assert body["available_mb"] == 0


async def test_capacity_requires_jwt(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, _ = client_and_conn
    resp = await client.get("/api/tenant/knowledge/capacity")
    assert resp.status_code == 401
