"""Admin impersonation endpoints -- Phase 9 PR-5 tests.

Coverage:
  - anon JWT denied on both endpoints (403).
  - record() accepts impersonating_kind/id kwargs (unit test).
"""

from __future__ import annotations

import inspect
import os
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

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


def _mint_jwt(*, role: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(uuid4()), "role": role, "iss": _TEST_JWT_ISSUER, "iat": now, "exp": now + 300},
        _TEST_JWT_SECRET,
        algorithm="HS256",
    )


class _FakeConn:
    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        pass

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


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


async def test_start_impersonation_anon_denied(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="admin")
    tid, uid = str(uuid4()), str(uuid4())
    r = await client.post(
        f"/api/admin/impersonate/{tid}/{uid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_stop_impersonation_anon_denied(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="admin")
    r = await client.delete(
        "/api/admin/impersonate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_audit_record_accepts_impersonating_kwargs() -> None:
    """Unit-level: record() signature includes impersonating_kind / impersonating_id."""
    from polaris_api.services.audit import record

    sig = inspect.signature(record)
    assert "impersonating_kind" in sig.parameters
    assert "impersonating_id" in sig.parameters
