"""Admin quota endpoints — Phase 9 PR-4 tests.

Coverage:
  * polaris_anon JWT role=admin cannot hit /api/admin/quotas/* (403).
  * Missing JWT returns 401.
  * GET /usage returns correct shape for platform_admin.
  * PUT /{tenant_id}/override with valid column sets it; audit row written.
  * PUT /{tenant_id}/override with invalid column returns 422.
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


class _FakeRecord:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d.get(key)


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.canned_rows: dict[str, list[dict[str, Any]]] = {}
        self.canned_row: dict[str, dict[str, Any] | None] = {}
        self.canned_val: dict[str, Any] = {}

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        for key, rows in self.canned_rows.items():
            if key in sql:
                return [_FakeRecord(r) for r in rows]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any | None:
        for key, row in self.canned_row.items():
            if key in sql:
                return _FakeRecord(row) if row is not None else None
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        for key, val in self.canned_val.items():
            if key in sql:
                return val
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"


@pytest_asyncio.fixture
async def fake_conn() -> AsyncIterator[_FakeConn]:
    conn = _FakeConn()

    async def override() -> AsyncIterator[_FakeConn]:
        yield conn

    app.dependency_overrides[get_admin_service_conn] = override
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------- Auth gate ----


async def test_missing_jwt_returns_401(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    res = await client.get("/api/admin/quotas/usage")
    assert res.status_code == 401


async def test_anon_role_denied_on_usage(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    token = _mint_jwt(role="anon")
    res = await client.get(
        "/api/admin/quotas/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


async def test_tenant_admin_role_denied(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    token = _mint_jwt(role="admin")
    res = await client.get(
        "/api/admin/quotas/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# ---------------------------------------------------------------- GET /usage ----


async def test_platform_admin_can_get_usage_empty(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    fake_conn.canned_rows["FROM tenants t"] = []
    token = _mint_jwt(role="platform_admin")
    res = await client.get(
        "/api/admin/quotas/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_usage_returns_tenant_row(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    tenant_id = uuid4()
    fake_conn.canned_rows["FROM tenants t"] = [
        {
            "tenant_id": tenant_id,
            "tenant_name": "Acme",
            "plan_tier": "pro",
            "bot_replies": 1000,
            "bot_replies_period_cap": 500,
            "vision_calls_per_month": 0,
            "conversation_retention_days": 30,
            "knowledge_chunks_total_mb": 100,
            "rate_limit_per_minute": 60,
            "monthly_llm_tokens_in": 1000000,
            "monthly_llm_tokens_out": 500000,
            "monthly_ingestion_pages": 200,
            "monthly_broadcast_recipients": 5000,
            "monthly_bookings": 100,
            "agent_seats_included": 1,
            "agent_seats_extra": 0,
            "outbound_broadcast_enabled": True,
            "multi_branch_enabled": False,
            "allowed_channel_kinds": ["whatsapp"],
            "usage_bot_replies_this_period": 250,
        }
    ]
    token = _mint_jwt(role="platform_admin")
    res = await client.get(
        "/api/admin/quotas/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["tenant_name"] == "Acme"
    assert row["bot_replies"] == 1000
    assert row["usage_bot_replies_this_period"] == 250
    # 250/500 * 100 = 50.0%
    assert row["usage_pct"] == 50.0


# ---------------------------------------------------------------- PUT override ----


async def test_override_invalid_column_returns_422(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    tenant_id = uuid4()
    token = _mint_jwt(role="platform_admin")
    res = await client.put(
        f"/api/admin/quotas/{tenant_id}/override",
        json={"column": "injected_column", "value": 999},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422


async def test_override_sets_column_and_writes_audit(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    tenant_id = uuid4()
    # tenant exists
    fake_conn.canned_row["SELECT id FROM tenants WHERE id"] = {"id": tenant_id}
    # current quota value for before-state
    fake_conn.canned_row["SELECT bot_replies FROM tenant_quotas"] = {"bot_replies": 500}
    # audit prev_hash
    fake_conn.canned_val["audit_log_prev_hash"] = None

    token = _mint_jwt(role="platform_admin")
    res = await client.put(
        f"/api/admin/quotas/{tenant_id}/override",
        json={"column": "bot_replies", "value": 2000},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["column"] == "bot_replies"
    assert body["new_value"] == 2000

    # Verify UPDATE and audit INSERT were executed.
    sqls = [s for s, _ in fake_conn.executed]
    assert any("UPDATE tenant_quotas" in s for s in sqls), "UPDATE not executed"
    assert any("INSERT INTO audit_logs" in s for s in sqls), "audit not written"


async def test_override_404_on_unknown_tenant(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    fake_conn.canned_row["SELECT id FROM tenants WHERE id"] = None
    token = _mint_jwt(role="platform_admin")
    res = await client.put(
        f"/api/admin/quotas/{uuid4()}/override",
        json={"column": "bot_replies", "value": 100},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404


async def test_override_rejects_sql_injection_via_column_name(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    tenant_id = uuid4()
    token = _mint_jwt(role="platform_admin")
    # Attempt column name with SQL injection payload — must be blocked at 422
    res = await client.put(
        f"/api/admin/quotas/{tenant_id}/override",
        json={"column": "bot_replies; DROP TABLE tenants;--", "value": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422
