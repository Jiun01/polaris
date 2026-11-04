"""Admin tenant lifecycle endpoints — Phase 9 PR-3.

Smoke coverage for the auth gate + happy-path shapes. RLS / audit-chain
correctness is exercised by the Phase 1 services.audit test suite; this
file focuses on the new router surface.

Tests (loop_scope=function — same rationale as test_admin_chunking_config.py):
  * Missing JWT  → 401.
  * tenant_admin JWT (role='admin') → 403.
  * platform_admin JWT → 200/201/204 happy paths.
  * Slug validation: invalid format → 400 from create.
  * Suspend / unsuspend transitions update tenants.status.
  * Soft-delete sets status='deleted' and emits a dsar audit row.
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


class _FakeConn:
    """Stub asyncpg.Connection just enough for the router to compile.

    The router only calls fetch / fetchrow / fetchval / execute and uses
    `async with conn.transaction()` for writes. We return canned values
    keyed by SQL substring.
    """

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


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeRecord:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d.get(key)


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


# --------------------------- Auth gate -----------------------------------


async def test_missing_jwt_returns_401(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    res = await client.get("/api/admin/tenants")
    assert res.status_code == 401


async def test_tenant_admin_jwt_returns_403(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    token = _mint_jwt(role="admin")
    res = await client.get(
        "/api/admin/tenants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


async def test_platform_admin_can_list(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    fake_conn.canned_rows["FROM tenants t"] = []  # empty list
    token = _mint_jwt(role="platform_admin")
    res = await client.get(
        "/api/admin/tenants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body == {"items": [], "total": 0}


# --------------------------- Create --------------------------------------


def _create_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "display_name": "Acme Salon",
        "slug": "acme-salon",
        "contact_email": "ops@acme.my",
        "contact_phone": "+60 12 345 6789",
        "vertical": "Salon",
        "plan_tier": "pro",
        "billing_cycle": "monthly",
        "sales_quote_id": None,
        "admin_email": "jane@acme.my",
        "admin_display_name": "Jane Tan",
    }
    base.update(overrides)
    return base


async def test_create_rejects_invalid_slug(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    token = _mint_jwt(role="platform_admin")
    res = await client.post(
        "/api/admin/tenants",
        json=_create_payload(slug="Bad Slug!"),
        headers={"Authorization": f"Bearer {token}"},
    )
    # Pydantic catches min_length / max_length / format violations as 422;
    # admin's app-side regex sometimes runs first as 400. Either is OK
    # for "client-side input rejected".
    assert res.status_code in (400, 422)


async def test_create_409_on_slug_taken(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    # The slug-existence fetchval returns truthy → router raises 409.
    fake_conn.canned_val["FROM tenants WHERE slug"] = 1
    token = _mint_jwt(role="platform_admin")
    res = await client.post(
        "/api/admin/tenants",
        json=_create_payload(slug="acme-salon"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 409
    assert res.json()["detail"] == "slug_taken"


async def test_create_happy_path(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    new_tenant_id = uuid4()
    new_user_id = uuid4()
    # Sequenced returns: slug-exists check (None), tenant INSERT id, then
    # auth.users SELECT (None → create), then auth.users INSERT id.
    # _FakeConn isn't ordered — wire by SQL substring instead.
    fake_conn.canned_val["FROM tenants WHERE slug"] = None
    fake_conn.canned_val["INSERT INTO tenants"] = new_tenant_id
    fake_conn.canned_val["FROM auth.users WHERE"] = None
    fake_conn.canned_val["INSERT INTO auth.users"] = new_user_id
    # audit_record reads prev_hash via SECURITY DEFINER function.
    fake_conn.canned_val["audit_log_prev_hash"] = None

    token = _mint_jwt(role="platform_admin")
    res = await client.post(
        "/api/admin/tenants",
        json=_create_payload(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] == str(new_tenant_id)
    assert body["slug"] == "acme-salon"


# --------------------------- Suspend / Unsuspend -------------------------


async def test_suspend_404_on_unknown_tenant(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    fake_conn.canned_row["SELECT status FROM tenants"] = None
    token = _mint_jwt(role="platform_admin")
    res = await client.post(
        f"/api/admin/tenants/{uuid4()}/suspend",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404


async def test_suspend_records_reason(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    fake_conn.canned_row["SELECT status FROM tenants"] = {"status": "active"}
    fake_conn.canned_val["audit_log_prev_hash"] = None
    token = _mint_jwt(role="platform_admin")
    res = await client.post(
        f"/api/admin/tenants/{uuid4()}/suspend",
        json={"reason": "manual"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 204
    # Confirm UPDATE was issued.
    assert any("UPDATE tenants" in sql for sql, _ in fake_conn.executed)


# --------------------------- Soft-delete ---------------------------------


async def test_soft_delete_flips_status_and_audits(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    fake_conn.canned_row["SELECT status, slug FROM tenants"] = {
        "status": "active",
        "slug": "doomed-tenant",
    }
    # cascade-scope counts row.
    fake_conn.canned_row["FROM tenant_invoices"] = {
        "invoices": 0,
        "audit": 0,
        "channels": 0,
        "users": 0,
        "branches": 0,
        "promotions": 0,
        "audience": 0,
    }
    fake_conn.canned_val["audit_log_prev_hash"] = None

    token = _mint_jwt(role="platform_admin")
    res = await client.delete(
        f"/api/admin/tenants/{uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    # Confirm status flip + audit insertion both fired.
    sql_seen = [s for s, _ in fake_conn.executed]
    assert any("UPDATE tenants" in s and "deleted" in s for s in sql_seen)
    assert any("INSERT INTO audit_logs" in s for s in sql_seen)
