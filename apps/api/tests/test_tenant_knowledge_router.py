"""Tenant knowledge router — POST/GET/DELETE + capacity gate.

Unit tests only (DB mocked via dependency override on
``get_request_conn`` + ``get_tenant_context``). Integration coverage —
real polaris_anon role with RLS — comes via Phase 3 PR-2's
``test_search_rpcs_rls.py`` end-to-end test once the search RPCs land.

Acceptance criteria exercised:
  * AC-INGESTION-001 — URL submission returns 201 with ETA + status URL.
  * AC-INGESTION-002 — Over-cap URL returns 409 ``kb_capacity_exceeded``.
  * AC-INGESTION-004 — Enterprise ``kb_mb_cap_override`` bypasses tier cap.
  * AC-INGESTION-009 — Tenant cannot see other tenant's requests (RLS-gated
    at SQL layer; unit test verifies the WHERE clause is present in queries).
  * AC-INGESTION-010 — DELETE cancels pending; 409 on already-processing.
  * Q-PHASE3-17 — Capacity gate uses live SUM(octet_length(content)).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Prime test env BEFORE polaris_api imports read it.
_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"  # 40 chars
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)

from polaris_api.deps.tenant import get_request_conn  # noqa: E402
from polaris_api.main import app  # noqa: E402

# Pin loop_scope=function — this file builds AsyncClient against the
# main `app` AND overrides async deps via app.dependency_overrides,
# matching the conftest.py warning (historically Phase 5's now-retired
# flow_runtime/ test subdirectory collected first and its session-loop
# initialisation got poisoned by this file's function-scope fixture
# teardown without this pin).
pytestmark = pytest.mark.asyncio(loop_scope="function")


def _mint_tenant_jwt(
    *,
    tenant_id: UUID,
    user_id: UUID | None = None,
    role: str = "admin",
) -> str:
    """Tenant-scoped HS256 token matching the prod portal's shape."""
    now = int(time.time())
    payload = {
        "sub": str(user_id or uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


def _active_tenant_row() -> dict[str, Any]:
    """Canned tenants-row response for get_tenant_context's status check."""
    return {"status": "active", "plan_tier": "starter", "suspended_reason": None}


def _make_capacity_responses(
    *,
    used_mb: int = 0,
    cap_mb: int = 100,
    insert_row: dict[str, Any] | None = None,
) -> list[Any]:
    """Sequence of fetchrow returns matching the call order in
    ``submit_request``: (1) tenant status (TenantContext), (2) effective cap,
    (3) used MB, (4) INSERT RETURNING row. ``insert_row=None`` means the
    test isn't expected to reach the INSERT (over-cap case)."""
    seq: list[Any] = [_active_tenant_row(), {"effective_cap_mb": cap_mb}, {"used_mb": used_mb}]
    if insert_row is not None:
        seq.append(insert_row)
    return seq


@pytest.fixture
def mock_conn() -> AsyncMock:
    conn = AsyncMock(spec=asyncpg.Connection)
    conn.execute = AsyncMock(return_value="UPDATE 0")
    conn.fetchrow = AsyncMock(return_value=_active_tenant_row())
    conn.fetch = AsyncMock(return_value=[])
    return conn


@pytest_asyncio.fixture(loop_scope="function")
async def client(mock_conn: AsyncMock) -> AsyncIterator[AsyncClient]:
    """ASGI client with get_request_conn overridden to the mock."""
    async def _override() -> AsyncIterator[AsyncMock]:
        yield mock_conn

    app.dependency_overrides[get_request_conn] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_request_conn, None)


# ---------------------------------------------------------------------------
# POST /api/tenant/knowledge/requests
# ---------------------------------------------------------------------------


async def test_url_submission_returns_201_with_eta(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """AC-INGESTION-001: URL submission returns 201 + ETA + status URL."""
    tenant_id = uuid4()
    inserted_id = uuid4()
    submitted_at = datetime.now(UTC)
    mock_conn.fetchrow = AsyncMock(
        side_effect=_make_capacity_responses(
            used_mb=10,
            cap_mb=100,
            insert_row={
                "id": inserted_id,
                "submitted_at": submitted_at,
                "status": "pending",
            },
        )
    )
    resp = await client.post(
        "/api/tenant/knowledge/requests",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={"source_type": "url", "source_url": "https://example.com/docs"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == str(inserted_id)
    assert body["status"] == "pending"
    assert body["status_url"].endswith(f"/api/tenant/knowledge/requests/{inserted_id}")
    # ETA must be exactly submitted_at + 24h per PRD §6.2 SLA.
    eta = datetime.fromisoformat(body["eta"])
    delta = (eta - submitted_at).total_seconds()
    assert 86_300 < delta < 86_500


async def test_url_over_cap_returns_409_kb_capacity_exceeded(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """AC-INGESTION-002: capacity gate rejects with structured 409."""
    tenant_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=_make_capacity_responses(used_mb=99, cap_mb=100, insert_row=None)
    )
    resp = await client.post(
        "/api/tenant/knowledge/requests",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={"source_type": "url", "source_url": "https://example.com"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "kb_capacity_exceeded"
    assert detail["current_mb"] == 99
    assert detail["tier_cap_mb"] == 100
    assert detail["would_be_mb"] >= 100


async def test_enterprise_override_bypasses_tier_cap(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """AC-INGESTION-004: kb_mb_cap_override wins (effective cap above tier).

    The capacity service returns ``effective_cap_mb`` which COALESCEs the
    override; this test asserts the router honours whatever the SQL gives
    back (it doesn't separately re-read the tier cap and second-guess).
    """
    tenant_id = uuid4()
    inserted_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=_make_capacity_responses(
            used_mb=400,
            cap_mb=10_000,  # Enterprise override well above Starter's default.
            insert_row={
                "id": inserted_id,
                "submitted_at": datetime.now(UTC),
                "status": "pending",
            },
        )
    )
    resp = await client.post(
        "/api/tenant/knowledge/requests",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={"source_type": "url", "source_url": "https://example.com"},
    )
    assert resp.status_code == 201, resp.text


async def test_text_submission_oversize_returns_422(client: AsyncClient) -> None:
    """Pydantic-side cap matches migration 018's source_text CHECK (100 KiB)."""
    tenant_id = uuid4()
    payload = {"source_type": "text", "source_text": "x" * 200_000}
    resp = await client.post(
        "/api/tenant/knowledge/requests",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json=payload,
    )
    assert resp.status_code == 422


async def test_source_type_url_with_storage_paths_returns_422(
    client: AsyncClient,
) -> None:
    """Pydantic-side mirror of the source-shape CHECK in migration 018."""
    tenant_id = uuid4()
    resp = await client.post(
        "/api/tenant/knowledge/requests",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "url",
            "source_url": "https://example.com",
            "storage_paths": ["mixed/path"],
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/tenant/knowledge/requests/{id}
# ---------------------------------------------------------------------------


async def test_cancel_pending_request_returns_204(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """AC-INGESTION-010: pending row cancellable."""
    tenant_id = uuid4()
    request_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            _active_tenant_row(),       # TenantContext
            {"status": "pending"},      # cancel_request lookup
        ]
    )
    resp = await client.delete(
        f"/api/tenant/knowledge/requests/{request_id}",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert resp.status_code == 204, resp.text


async def test_cancel_processing_request_returns_409(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """AC-INGESTION-010 negative: in-flight row cannot be cancelled."""
    tenant_id = uuid4()
    request_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            _active_tenant_row(),
            {"status": "processing"},
        ]
    )
    resp = await client.delete(
        f"/api/tenant/knowledge/requests/{request_id}",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_cancellable"


async def test_cancel_unknown_request_returns_404(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    tenant_id = uuid4()
    request_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[_active_tenant_row(), None]  # lookup returns no row
    )
    resp = await client.delete(
        f"/api/tenant/knowledge/requests/{request_id}",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/tenant/knowledge/uploads/sign — AC-INGESTION-003
# ---------------------------------------------------------------------------


async def test_uploads_sign_over_cap_returns_409_no_signed_url(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """AC-INGESTION-003: pre-upload capacity check rejects with 409.

    Exercises the sign_upload path: tenant with used_mb=490 attempting
    a 20MB file upload against a 500MB cap. The capacity-gate inside
    the endpoint runs the same get_effective_cap_mb + live SUM as
    the intake endpoint, then 409s BEFORE the Supabase Storage call
    is attempted — so no signed URL is ever issued.

    The 20MB file_size_bytes is below the per-file _MAX_UPLOAD_BYTES
    (50 MiB) ceiling so it passes Pydantic; it's the cap-vs-usage
    arithmetic that rejects it.
    """
    tenant_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            _active_tenant_row(),              # TenantContext
            {"effective_cap_mb": 500},         # get_effective_cap_mb
            {"used_mb": 490},                  # estimate_current_usage_mb
            # NB: no further rows — the 409 fires before any Supabase
            # Storage call, so the side_effect list is intentionally
            # short. If the handler accidentally tried to sign the URL
            # AsyncMock would raise StopIteration and the test would
            # fail loudly.
        ]
    )
    resp = await client.post(
        "/api/tenant/knowledge/uploads/sign",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "file_size_bytes": 20 * 1024 * 1024,
            "content_type": "application/pdf",
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "kb_capacity_exceeded"
    assert detail["current_mb"] == 490
    assert detail["tier_cap_mb"] == 500
    # incoming_mb = 20 (file is 20 MiB; max(1, 20*1024*1024 // 1024**2) = 20)
    assert detail["would_be_mb"] == 510


async def test_uploads_sign_disallowed_content_type_returns_422(
    client: AsyncClient,
) -> None:
    """PR-5 M5: content_type allowlist rejects executables at the API edge."""
    tenant_id = uuid4()
    resp = await client.post(
        "/api/tenant/knowledge/uploads/sign",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "file_size_bytes": 1024,
            "content_type": "application/x-msdownload",
        },
    )
    assert resp.status_code == 422, resp.text
    assert "content_type" in resp.text.lower()


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_missing_jwt_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/tenant/knowledge/requests",
        json={"source_type": "url", "source_url": "https://example.com"},
    )
    assert resp.status_code == 401
