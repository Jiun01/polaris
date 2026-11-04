"""Admin financials endpoints — Phase 9 PR-11 tests.

Coverage:
  * Auth gate: missing JWT → 401, anon role → 403.
  * GET /mrr: returns latest rollup + band + trend_30d.
  * POST /mrr/recompute: triggers recomputation, returns new result.
  * Anonymous rejected (401/403).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="function")

_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)
import jwt  # noqa: E402
from polaris_api.main import app  # noqa: E402
from polaris_api.services.admin import get_admin_service_conn  # noqa: E402


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


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, mrr: float = 12_000.0) -> None:
        self._mrr = mrr

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetchval(self, sql: str, *args: Any) -> float:
        return self._mrr

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        return "OK"


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


_ADMIN_TOKEN = _mint_jwt(role="platform_admin", sub=uuid4())
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_missing_jwt_get_mrr_returns_401(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    res = await client.get("/api/admin/financials/mrr")
    assert res.status_code == 401


async def test_anon_role_get_mrr_denied(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    token = _mint_jwt(role="anon")
    res = await client.get(
        "/api/admin/financials/mrr",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


async def test_missing_jwt_recompute_returns_401(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    res = await client.post("/api/admin/financials/mrr/recompute")
    assert res.status_code == 401


async def test_anon_role_recompute_denied(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    token = _mint_jwt(role="anon")
    res = await client.post(
        "/api/admin/financials/mrr/recompute",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# GET /mrr
# ---------------------------------------------------------------------------


async def test_get_mrr_returns_latest_rollup(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """GET /mrr triggers a recompute if no prior snapshot, returns band + trend."""
    res = await client.get("/api/admin/financials/mrr", headers=_ADMIN_HEADERS)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "mrr_myr" in data
    assert "alert_band" in data
    assert "computed_at" in data
    assert "trend_30d" in data
    # Fake conn returns 12 000 MYR → green
    assert data["alert_band"] == "green"
    assert data["mrr_myr"] == pytest.approx(12_000.0)


async def test_get_mrr_band_amber(client: AsyncClient) -> None:
    """Band correctly reflects amber when DB returns value in amber range."""
    amber_conn = _FakeConn(mrr=40_000.0)

    async def _override() -> AsyncIterator[_FakeConn]:
        yield amber_conn

    app.dependency_overrides[get_admin_service_conn] = _override
    try:
        res = await client.get("/api/admin/financials/mrr", headers=_ADMIN_HEADERS)
        assert res.status_code == 200
        assert res.json()["alert_band"] == "amber"
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


async def test_get_mrr_band_red(client: AsyncClient) -> None:
    """Band correctly reflects red when DB returns value above the red boundary."""
    red_conn = _FakeConn(mrr=45_000.0)

    async def _override() -> AsyncIterator[_FakeConn]:
        yield red_conn

    app.dependency_overrides[get_admin_service_conn] = _override
    try:
        res = await client.get("/api/admin/financials/mrr", headers=_ADMIN_HEADERS)
        assert res.status_code == 200
        assert res.json()["alert_band"] == "red"
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


# ---------------------------------------------------------------------------
# POST /mrr/recompute
# ---------------------------------------------------------------------------


async def test_recompute_returns_fresh_result(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """POST /mrr/recompute runs compute_and_persist and returns result."""
    res = await client.post(
        "/api/admin/financials/mrr/recompute", headers=_ADMIN_HEADERS
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert "mrr_myr" in data
    assert "alert_band" in data
    assert "computed_at" in data
    assert "trend_30d" in data


async def test_recompute_returns_updated_values(client: AsyncClient) -> None:
    """Recompute reflects the current DB state, not a stale cache."""
    live_conn = _FakeConn(mrr=45_000.0)

    async def _override() -> AsyncIterator[_FakeConn]:
        yield live_conn

    app.dependency_overrides[get_admin_service_conn] = _override
    try:
        res = await client.post(
            "/api/admin/financials/mrr/recompute", headers=_ADMIN_HEADERS
        )
        data = res.json()
        assert data["mrr_myr"] == pytest.approx(45_000.0)
        assert data["alert_band"] == "red"
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)
