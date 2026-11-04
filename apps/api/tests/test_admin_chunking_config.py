"""Admin chunking-config endpoint — service-role gate + JSONB projection.

GET /api/admin/tenants/{tenant_id}/chunking-config

Tests:
  * Service-role JWT (role='platform_service') gets 200 + ChunkingConfig.
  * tenant_admin JWT (role='admin') gets 403 — not 401 — so the portal
    can distinguish "wrong scope" from "expired token".
  * Missing JWT returns 401.
  * Tenant with no settings JSONB falls back to ADR-032 Amendment
    defaults (512 / 80 / true).
  * Tenant with per-tenant override returns the override.
  * Unknown tenant returns 404.
  * Bad settings (overlap >= size) returns 500.

The connection is supplied via FastAPI's ``Depends(get_admin_service_conn)`` so
tests override via ``app.dependency_overrides`` — same pattern the
existing test_tenant_context.py uses, which keeps pytest-asyncio's
session event loop intact.
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

# Pin loop_scope=function for every test in this file. See conftest.py's
# top-level docstring on `_LOOP_SCOPE_KNOWN_ISSUE` — empirically (verified
# in PR-5 review M6) admin test files that build an ASGITransport on the
# main `app` AND override async deps via dependency_overrides poison the
# session-scoped event loop on teardown, surfacing as "no current event
# loop" in every subsequent test. The shape of the offending interaction
# isn't fully understood; this pin is the working remediation until
# pytest-asyncio's session-loop handling can be debugged in isolation.
pytestmark = pytest.mark.asyncio(loop_scope="function")

# Prime test env before polaris_api imports read it.
_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)

from polaris_api.main import app  # noqa: E402
from polaris_api.services.admin import get_admin_service_conn  # noqa: E402


def _mint_jwt(*, role: str, tenant_id: UUID | None = None) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_conn() -> AsyncIterator[tuple[AsyncClient, AsyncMock]]:
    """Override ``get_admin_service_conn`` to yield a mock asyncpg connection.

    Mirrors test_tenant_context.py's dependency-override pattern. ``loop_scope``
    is pinned to ``function`` so the fixture lives on its own per-test event
    loop instead of the session loop shared with conftest's session-scoped
    asyncpg fixtures — that sharing leaked a "no current event loop" failure
    into every test that ran after this file. Pytest-asyncio happily runs
    function-loop fixtures alongside session-loop ones in the same suite.
    """
    conn = AsyncMock()

    async def _override() -> AsyncIterator[AsyncMock]:
        yield conn

    app.dependency_overrides[get_admin_service_conn] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, conn
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_service_role_jwt_returns_200(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetchrow = AsyncMock(return_value={"settings": {}})

    resp = await client.get(
        f"/api/admin/tenants/{tenant_id}/chunking-config",
        headers={"Authorization": f"Bearer {_mint_jwt(role='platform_service')}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ADR-032 Amendment defaults.
    assert body["chunk_size"] == 512
    assert body["chunk_overlap"] == 80
    assert body["contextual_enrichment_default"] is True


async def test_tenant_admin_jwt_returns_403(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """Tenant tokens land on /api/admin/* → 403, not 401."""
    client, _ = client_and_conn
    tenant_id = uuid4()
    resp = await client.get(
        f"/api/admin/tenants/{tenant_id}/chunking-config",
        headers={
            "Authorization": f"Bearer {_mint_jwt(role='admin', tenant_id=tenant_id)}"
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "admin_role_required"


async def test_missing_jwt_returns_401(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, _ = client_and_conn
    tenant_id = uuid4()
    resp = await client.get(f"/api/admin/tenants/{tenant_id}/chunking-config")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# JSONB projection
# ---------------------------------------------------------------------------


async def test_tenant_settings_override_returned(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """Per-tenant override in tenants.settings wins over the defaults."""
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetchrow = AsyncMock(
        return_value={
            "settings": {
                "rag_chunk_size": 1024,
                "rag_chunk_overlap": 128,
                "rag_contextual_enrichment": False,
            }
        }
    )
    resp = await client.get(
        f"/api/admin/tenants/{tenant_id}/chunking-config",
        headers={"Authorization": f"Bearer {_mint_jwt(role='platform_service')}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunk_size"] == 1024
    assert body["chunk_overlap"] == 128
    assert body["contextual_enrichment_default"] is False


async def test_unknown_tenant_returns_404(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetchrow = AsyncMock(return_value=None)
    resp = await client.get(
        f"/api/admin/tenants/{tenant_id}/chunking-config",
        headers={"Authorization": f"Bearer {_mint_jwt(role='platform_service')}"},
    )
    assert resp.status_code == 404


async def test_settings_overlap_exceeds_size_returns_500(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """Bad settings (overlap >= size) trips the ChunkingConfig validator.

    Surfaces as 500 because the data error is platform-side, not tenant-side
    (the desktop never causes this — only an admin mis-edit can).
    """
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetchrow = AsyncMock(
        return_value={
            "settings": {"rag_chunk_size": 100, "rag_chunk_overlap": 100}
        }
    )
    resp = await client.get(
        f"/api/admin/tenants/{tenant_id}/chunking-config",
        headers={"Authorization": f"Bearer {_mint_jwt(role='platform_service')}"},
    )
    assert resp.status_code == 500
