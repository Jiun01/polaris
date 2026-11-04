"""Admin website-leads endpoints tests.

Unit-level (fake asyncpg conn, no real DB — same posture as test_admin_audit):
  - auth gates (401 no JWT, 403 non-admin role)
  - list returns 200 with items/next_cursor shape + row conversion
  - query-param validation (invalid status/form_type -> 422)
  - mark-contacted: 404 when missing, 200 returns updated item
  - export: text/csv with header + flattened detail columns (bool -> yes/no)
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="function")

_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)

from polaris_api.main import app  # noqa: E402
from polaris_api.services.admin.pool import get_admin_service_conn  # noqa: E402


def _mint_jwt(*, role: str, sub: UUID | None = None) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(sub or uuid4()),
        "platform_admin_id": str(sub or uuid4()),
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


def _lead_row(**overrides: Any) -> dict[str, Any]:
    """A dict shaped like a website_leads row (dict supports row["k"] like asyncpg.Record)."""
    base: dict[str, Any] = {
        "id": uuid4(),
        "form_type": "pricing",
        "name": "Nurul Aina",
        "email": "nurul@kedaiaina.my",
        "phone": "+60123456789",
        "business_name": "Kedai Aina",
        "industry": "Retail / Fashion",
        "message": None,
        "details": {"plan": "pro", "heard_from": "google", "consent": True},
        "status": "new",
        "contacted_at": None,
        "created_at": datetime(2026, 6, 21, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return base


class _FakeConn:
    def __init__(self) -> None:
        self.canned_rows: list[Any] = []
        self.canned_row: Any = None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.canned_rows

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self.canned_row


@pytest_asyncio.fixture
async def fake_conn() -> AsyncIterator[_FakeConn]:
    conn = _FakeConn()

    async def _override() -> AsyncIterator[_FakeConn]:
        yield conn

    app.dependency_overrides[get_admin_service_conn] = _override
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_jwt(role='platform_admin')}"}


# --------------- auth gate ---------------


async def test_list_missing_jwt_401(client: AsyncClient, fake_conn: _FakeConn) -> None:
    r = await client.get("/api/admin/leads/")
    assert r.status_code == 401


async def test_list_non_admin_role_403(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="tenant_admin")
    r = await client.get("/api/admin/leads/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# --------------- list ---------------


async def test_list_empty_200(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows = []
    r = await client.get("/api/admin/leads/", headers=_admin_headers())
    assert r.status_code == 200
    data = r.json()
    assert data == {"items": [], "next_cursor": None}


async def test_list_returns_items(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows = [_lead_row()]
    r = await client.get("/api/admin/leads/", headers=_admin_headers())
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["form_type"] == "pricing"
    assert item["name"] == "Nurul Aina"
    assert item["details"]["plan"] == "pro"
    assert item["status"] == "new"
    assert item["contacted_at"] is None


async def test_list_invalid_form_type_422(client: AsyncClient, fake_conn: _FakeConn) -> None:
    r = await client.get("/api/admin/leads/?form_type=nope", headers=_admin_headers())
    assert r.status_code == 422


async def test_list_invalid_status_422(client: AsyncClient, fake_conn: _FakeConn) -> None:
    r = await client.get("/api/admin/leads/?status=archived", headers=_admin_headers())
    assert r.status_code == 422


async def test_list_contacted_view_200(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows = [
        _lead_row(status="contacted", contacted_at=datetime(2026, 6, 21, 13, 0, tzinfo=UTC))
    ]
    r = await client.get("/api/admin/leads/?status=contacted", headers=_admin_headers())
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["status"] == "contacted"
    assert item["contacted_at"] is not None


# --------------- mark contacted ---------------


async def test_mark_contacted_404(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row = None
    r = await client.patch(f"/api/admin/leads/{uuid4()}/contacted", headers=_admin_headers())
    assert r.status_code == 404


async def test_mark_contacted_200(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row = _lead_row(
        status="contacted", contacted_at=datetime(2026, 6, 21, 14, 0, tzinfo=UTC)
    )
    r = await client.patch(f"/api/admin/leads/{uuid4()}/contacted", headers=_admin_headers())
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "contacted"
    assert data["contacted_at"] is not None


# --------------- export ---------------


async def test_export_returns_csv(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows = [_lead_row()]
    r = await client.get("/api/admin/leads/export", headers=_admin_headers())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    lines = body.strip().splitlines()
    assert lines[0].startswith("created_at,form_type,name,email")
    assert "Nurul Aina" in body
    assert "pro" in body  # flattened details.plan column
    assert "yes" in body  # details.consent bool -> yes


async def test_export_empty_header_only(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows = []
    r = await client.get("/api/admin/leads/export", headers=_admin_headers())
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert len(lines) == 1  # header row only
