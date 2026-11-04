"""Admin audit endpoints -- Phase 9 PR-5 tests.

Coverage:
  - polaris_anon role denied on all endpoints (403).
  - Missing JWT returns 401.
  - platform_admin can list logs (200 with items key).
  - GET /verifications/latest returns 404 when no rows.
  - POST /verify returns 200 with status=triggered.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
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
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


class _FakeConn:
    """Minimal asyncpg stub for unit-level router tests."""

    def __init__(self) -> None:
        self.canned_rows: list[Any] = []
        self.canned_row: Any = None
        self.canned_val: Any = None

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.canned_rows

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self.canned_row

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self.canned_val

    async def execute(self, sql: str, *args: Any) -> None:
        pass


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


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


# --------------- auth gate ---------------


async def test_missing_jwt_returns_401(client: AsyncClient, fake_conn: _FakeConn) -> None:
    r = await client.get("/api/admin/audit/logs")
    assert r.status_code == 401


async def test_anon_role_denied_logs(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="admin")
    r = await client.get("/api/admin/audit/logs", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_anon_role_denied_verifications(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="admin")
    r = await client.get("/api/admin/audit/verifications/latest", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_anon_role_denied_verify_post(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="admin")
    r = await client.post("/api/admin/audit/verify", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# --------------- happy paths ---------------


async def test_list_audit_logs_returns_empty(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """platform_admin token returns 200 with items list."""
    fake_conn.canned_rows = []
    token = _mint_jwt(role="platform_admin")
    r = await client.get("/api/admin/audit/logs", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert data["next_cursor"] is None


async def test_get_latest_verification_404_when_none(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """GET /verifications/latest returns 404 when no rows exist."""
    fake_conn.canned_row = None
    token = _mint_jwt(role="platform_admin")
    r = await client.get("/api/admin/audit/verifications/latest", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


async def test_trigger_verify_returns_triggered(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """POST /verify returns 200 with status=triggered."""
    from unittest.mock import MagicMock, patch

    mock_pool = MagicMock()

    with patch("polaris_api.routers.admin_audit.get_service_pool", return_value=mock_pool):
        token = _mint_jwt(role="platform_admin")
        r = await client.post("/api/admin/audit/verify", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "triggered"
