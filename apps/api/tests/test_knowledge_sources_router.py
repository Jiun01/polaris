"""Knowledge-sources router — POST/GET/DELETE + capacity gate.

Unit tests only (DB mocked via dependency override on
``get_request_conn`` + ``get_tenant_context``). Mirrors the fixture
pattern established in ``test_tenant_knowledge_router.py``.

Acceptance criteria exercised:
  * POST /sources (text) → 201, row returned with status='pending'.
  * POST /sources over capacity → 409 ``kb_capacity_exceeded``.
  * GET /sources → excludes soft-deleted rows.
  * DELETE /sources/{id} → 204; subsequent GET no longer lists it.
  * Source-shape validation: POST with source_type='url' but no
    source_url → 422 (Pydantic-side mirror of migration 118 CHECK).
"""

from __future__ import annotations

import hashlib
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

# Pin loop_scope=function — same rationale as test_tenant_knowledge_router.py.
pytestmark = pytest.mark.asyncio(loop_scope="function")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mint_tenant_jwt(*, tenant_id: UUID, role: str = "admin") -> str:
    now = int(time.time())
    payload = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


def _active_tenant_row() -> dict[str, Any]:
    return {"status": "active", "plan_tier": "starter", "suspended_reason": None}


def _source_row(
    *,
    source_id: UUID | None = None,
    tenant_id: UUID | None = None,
    source_type: str = "text",
    status: str = "pending",
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": source_id or uuid4(),
        "tenant_id": tenant_id or uuid4(),
        "tenant_branch_id": None,
        "source_type": source_type,
        "source_url": None,
        "storage_path": None,
        "source_label": "My source",
        "status": status,
        "chunk_count": 0,
        "last_synced_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _make_capacity_responses(
    *,
    used_mb: int = 0,
    cap_mb: int = 100,
    insert_row: dict[str, Any] | None = None,
) -> list[Any]:
    """Sequence of fetchrow returns for insert_source:
      (1) tenant status (TenantContext),
      (2) effective cap,
      (3) used MB,
      (4) INSERT RETURNING row  — omit for over-cap case.
    """
    seq: list[Any] = [
        _active_tenant_row(),
        {"effective_cap_mb": cap_mb},
        {"used_mb": used_mb},
    ]
    if insert_row is not None:
        seq.append(insert_row)
    return seq


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
# POST /api/tenant/knowledge/sources
# ---------------------------------------------------------------------------


async def test_post_text_source_returns_201_with_pending_status(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """POST /sources (text) → 201, row returned with status='pending'."""
    tenant_id = uuid4()
    source_text = "Our refund policy is 30 days."
    source_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=_make_capacity_responses(
            used_mb=5,
            cap_mb=100,
            insert_row=_source_row(
                source_id=source_id, tenant_id=tenant_id, source_type="text"
            ),
        )
    )
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "text",
            "source_text": source_text,
            "source_label": "Refund policy",
            "desired_hash": _sha256(source_text),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == str(source_id)
    assert body["status"] == "pending"
    assert body["chunk_count"] == 0
    assert body["last_synced_at"] is None
    assert body["source_type"] == "text"


async def test_post_url_source_returns_201(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """POST /sources (url) → 201."""
    tenant_id = uuid4()
    url = "https://example.com/faq"
    source_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=_make_capacity_responses(
            used_mb=0,
            cap_mb=100,
            insert_row=_source_row(
                source_id=source_id,
                tenant_id=tenant_id,
                source_type="url",
            ),
        )
    )
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "url",
            "source_url": "https://example.com/faq",
            "source_label": "FAQ page",
            "desired_hash": _sha256(url),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"


async def test_post_source_over_capacity_returns_409(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """POST /sources over cap → 409 kb_capacity_exceeded with structured detail."""
    tenant_id = uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=_make_capacity_responses(
            used_mb=99,
            cap_mb=100,
            insert_row=None,  # should not be reached
        )
    )
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "url",
            "source_url": "https://example.com",
            "source_label": "Over-cap URL",
            "desired_hash": _sha256("https://example.com"),
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "kb_capacity_exceeded"
    assert detail["current_mb"] == 99
    assert detail["tier_cap_mb"] == 100
    assert detail["would_be_mb"] >= 100
    assert "hint" in detail


# ---------------------------------------------------------------------------
# GET /api/tenant/knowledge/sources
# ---------------------------------------------------------------------------


async def test_get_sources_returns_active_rows(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """GET /sources → returns active rows (list, newest first)."""
    tenant_id = uuid4()
    row1 = _source_row(tenant_id=tenant_id, source_type="url")
    row2 = _source_row(tenant_id=tenant_id, source_type="text")
    mock_conn.fetchrow = AsyncMock(return_value=_active_tenant_row())
    mock_conn.fetch = AsyncMock(return_value=[row1, row2])

    resp = await client.get(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert body[0]["id"] == str(row1["id"])
    assert body[1]["id"] == str(row2["id"])


async def test_get_sources_excludes_soft_deleted(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """GET /sources excludes soft-deleted rows (deleted_at IS NOT NULL).

    The service layer filters in SQL; we verify the router returns only what
    the service returns — the mock returns an empty list to simulate all rows
    being deleted.
    """
    tenant_id = uuid4()
    mock_conn.fetchrow = AsyncMock(return_value=_active_tenant_row())
    # SQL filters deleted; mock returns empty list for this tenant.
    mock_conn.fetch = AsyncMock(return_value=[])

    resp = await client.get(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# DELETE /api/tenant/knowledge/sources/{id}
# ---------------------------------------------------------------------------


async def test_delete_source_returns_204(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """DELETE /sources/{id} → 204 when row exists and is active."""
    tenant_id = uuid4()
    source_id = uuid4()
    mock_conn.fetchrow = AsyncMock(return_value=_active_tenant_row())
    # Simulate UPDATE returning "UPDATE 1" — one row soft-deleted.
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    resp = await client.delete(
        f"/api/tenant/knowledge/sources/{source_id}",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert resp.status_code == 204, resp.text


async def test_delete_source_not_found_returns_404(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """DELETE /sources/{id} → 404 when no live row matches."""
    tenant_id = uuid4()
    source_id = uuid4()
    mock_conn.fetchrow = AsyncMock(return_value=_active_tenant_row())
    # UPDATE matched 0 rows — source already deleted or never existed.
    mock_conn.execute = AsyncMock(return_value="UPDATE 0")

    resp = await client.delete(
        f"/api/tenant/knowledge/sources/{source_id}",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "source_not_found"


async def test_delete_then_get_no_longer_lists_source(
    client: AsyncClient, mock_conn: AsyncMock
) -> None:
    """DELETE then GET → source no longer appears in list.

    Simulates: soft-delete sets deleted_at, subsequent GET returns SQL rows
    excluding deleted (mock returns empty list post-delete).
    """
    tenant_id = uuid4()
    source_id = uuid4()

    # Step 1: DELETE — simulate successful soft-delete.
    mock_conn.fetchrow = AsyncMock(return_value=_active_tenant_row())
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    del_resp = await client.delete(
        f"/api/tenant/knowledge/sources/{source_id}",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert del_resp.status_code == 204, del_resp.text

    # Step 2: GET — SQL WHERE deleted_at IS NULL now excludes it; mock empty.
    mock_conn.fetch = AsyncMock(return_value=[])
    get_resp = await client.get(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json() == []


# ---------------------------------------------------------------------------
# Source-shape validation (Pydantic-side mirror of migration 118 CHECK)
# ---------------------------------------------------------------------------


async def test_url_type_without_source_url_returns_422(
    client: AsyncClient,
) -> None:
    """source_type='url' with no source_url → 422 (shape validator)."""
    tenant_id = uuid4()
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "url",
            # source_url omitted
            "source_label": "Missing URL",
            "desired_hash": "abc123",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_url_type_with_storage_path_returns_422(
    client: AsyncClient,
) -> None:
    """source_type='url' with extra storage_path → 422 (shape validator)."""
    tenant_id = uuid4()
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "url",
            "source_url": "https://example.com",
            "storage_path": "tenant-id/some-file",
            "source_label": "Mixed shape",
            "desired_hash": "abc123",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_text_type_without_source_text_returns_422(
    client: AsyncClient,
) -> None:
    """source_type='text' with no source_text → 422."""
    tenant_id = uuid4()
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "text",
            # source_text omitted
            "source_label": "No body",
            "desired_hash": "abc123",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_text_oversize_returns_422(client: AsyncClient) -> None:
    """source_text > 100 KiB → 422 (Pydantic max_length mirrors SQL CHECK)."""
    tenant_id = uuid4()
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "text",
            "source_text": "x" * 200_000,
            "source_label": "Huge text",
            "desired_hash": "abc123",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_file_type_without_storage_path_returns_422(
    client: AsyncClient,
) -> None:
    """source_type='file' with no storage_path → 422."""
    tenant_id = uuid4()
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        headers={"Authorization": f"Bearer {_mint_tenant_jwt(tenant_id=tenant_id)}"},
        json={
            "source_type": "file",
            # storage_path omitted
            "source_label": "Missing path",
            "desired_hash": "abc123",
        },
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_missing_jwt_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/tenant/knowledge/sources",
        json={
            "source_type": "text",
            "source_text": "hello",
            "source_label": "test",
            "desired_hash": "abc",
        },
    )
    assert resp.status_code == 401
