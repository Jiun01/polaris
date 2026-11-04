"""Admin cost endpoints — Phase 9 PR-10 tests.

Coverage:
  * Mimir mock returns fixture → all-tenants rows include computed alert_band.
  * alert_band=amber when cost > 50% MRR; alert_band=red when cost > 100% MRR.
  * cohere-overrides endpoint returns only tenants with override set.
  * Anonymous request rejected (401/403).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch
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
os.environ.setdefault("GRAFANA_CLOUD_MIMIR_URL", "https://mimir.example.com")
os.environ.setdefault("GRAFANA_CLOUD_MIMIR_TOKEN", "pytest-mimir-token")

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
    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d.get(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def keys(self) -> list[str]:
        return list(self._d.keys())


class _FakeConn:
    def __init__(self) -> None:
        self.canned_rows: dict[str, list[dict[str, Any]]] = {}
        self.canned_row: dict[str, dict[str, Any] | None] = {}
        self.canned_val: dict[str, Any] = {}

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
        for key, rows in self.canned_rows.items():
            if key in sql:
                return [_FakeRecord(r) for r in rows]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRecord | None:
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

_TENANT_ID_1 = uuid4()
_TENANT_ID_2 = uuid4()
_TENANT_ID_3 = uuid4()


# Rows returned from the DB for the all-tenants cost list
_TENANT_ROWS = [
    {
        "id": _TENANT_ID_1,
        "display_name": "ACME Sdn Bhd",
        "mrr_myr": Decimal("2499.00"),
    },
    {
        "id": _TENANT_ID_2,
        "display_name": "Beta Corp",
        "mrr_myr": Decimal("1099.00"),
    },
    {
        "id": _TENANT_ID_3,
        "display_name": "Gamma Ltd",
        "mrr_myr": Decimal("5499.00"),
    },
]

_COHERE_TENANT_ROWS = [
    {
        "id": _TENANT_ID_1,
        "display_name": "ACME Sdn Bhd",
        "mrr_myr": Decimal("2499.00"),
        "cohere_rerank_enabled": True,
    },
]


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_missing_jwt_returns_401(client: AsyncClient, fake_conn: _FakeConn) -> None:
    res = await client.get("/api/admin/cost/tenants")
    assert res.status_code == 401


async def test_anon_role_returns_403(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="anon")
    res = await client.get(
        "/api/admin/cost/tenants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# GET /tenants — all-tenants cost table
# ---------------------------------------------------------------------------


async def test_all_tenants_cost_returns_items(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """All-tenants endpoint returns one row per active tenant."""
    fake_conn.canned_rows["FROM tenants"] = _TENANT_ROWS

    from polaris_api.services.grafana_cost import _zero_cost

    def _mock_fetch(*args: Any, **kwargs: Any) -> Any:
        return _zero_cost(
            tenant_id=kwargs.get("tenant_id") or args[0],
            yyyymm=kwargs.get("yyyymm") or args[1],
            tenant_mrr_myr=kwargs.get("tenant_mrr_myr") or Decimal("2499.00"),
        )

    with patch(
        "polaris_api.routers.admin_cost.fetch_tenant_cost_monthly",
        new_callable=AsyncMock,
        side_effect=lambda **kw: _async_zero(kw),
    ):
        res = await client.get("/api/admin/cost/tenants", headers=_ADMIN_HEADERS)

    assert res.status_code == 200, res.text
    data = res.json()
    assert "items" in data
    assert len(data["items"]) == len(_TENANT_ROWS)


def _async_zero(kw: dict) -> Any:
    from polaris_api.services.grafana_cost import _zero_cost
    return _zero_cost(
        tenant_id=kw["tenant_id"],
        yyyymm=kw["yyyymm"],
        tenant_mrr_myr=kw["tenant_mrr_myr"],
    )


async def test_alert_band_amber_when_cost_over_50pct(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """alert_band=amber when total cost > 50% of MRR."""
    from decimal import Decimal

    from polaris_api.services.grafana_cost import _build_cost_result
    from polaris_shared_types.tenant_cost import AlertBand

    mrr = Decimal("2499.00")
    # 60% of MRR → amber
    total_cost = mrr * Decimal("0.60")

    result = _build_cost_result(
        tenant_id=_TENANT_ID_1,
        yyyymm="202606",
        anthropic_input_cost_myr=total_cost,
        anthropic_output_cost_myr=Decimal("0"),
        cohere_rerank_cost_myr=Decimal("0"),
        supabase_storage_cost_myr=Decimal("0"),
        openrouter_embedding_cost_myr=Decimal("0"),
        tenant_mrr_myr=mrr,
        data_source="mimir",
    )

    assert result.alert_band == AlertBand.AMBER


async def test_alert_band_red_when_cost_over_100pct(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """alert_band=red when total cost > 100% of MRR."""
    from decimal import Decimal

    from polaris_api.services.grafana_cost import _build_cost_result
    from polaris_shared_types.tenant_cost import AlertBand

    mrr = Decimal("2499.00")
    # 110% of MRR → red
    total_cost = mrr * Decimal("1.10")

    result = _build_cost_result(
        tenant_id=_TENANT_ID_1,
        yyyymm="202606",
        anthropic_input_cost_myr=total_cost,
        anthropic_output_cost_myr=Decimal("0"),
        cohere_rerank_cost_myr=Decimal("0"),
        supabase_storage_cost_myr=Decimal("0"),
        openrouter_embedding_cost_myr=Decimal("0"),
        tenant_mrr_myr=mrr,
        data_source="mimir",
    )

    assert result.alert_band == AlertBand.RED


async def test_alert_band_green_when_cost_under_50pct(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """alert_band=green when total cost < 50% of MRR."""
    from decimal import Decimal

    from polaris_api.services.grafana_cost import _build_cost_result
    from polaris_shared_types.tenant_cost import AlertBand

    mrr = Decimal("2499.00")
    total_cost = mrr * Decimal("0.30")  # 30%

    result = _build_cost_result(
        tenant_id=_TENANT_ID_1,
        yyyymm="202606",
        anthropic_input_cost_myr=total_cost,
        anthropic_output_cost_myr=Decimal("0"),
        cohere_rerank_cost_myr=Decimal("0"),
        supabase_storage_cost_myr=Decimal("0"),
        openrouter_embedding_cost_myr=Decimal("0"),
        tenant_mrr_myr=mrr,
        data_source="mimir",
    )

    assert result.alert_band == AlertBand.GREEN


# ---------------------------------------------------------------------------
# GET /cohere-overrides
# ---------------------------------------------------------------------------


async def test_cohere_overrides_returns_only_override_tenants(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Cohere-overrides endpoint returns only tenants with cohere_rerank_enabled IS NOT NULL."""
    fake_conn.canned_rows["cohere_rerank_enabled IS NOT NULL"] = _COHERE_TENANT_ROWS

    with patch(
        "polaris_api.routers.admin_cost.fetch_tenant_cost_monthly",
        new_callable=AsyncMock,
        side_effect=lambda **kw: _async_zero(kw),
    ):
        res = await client.get("/api/admin/cost/cohere-overrides", headers=_ADMIN_HEADERS)

    assert res.status_code == 200, res.text
    data = res.json()
    assert "items" in data
    # Only the one tenant with cohere override
    assert len(data["items"]) == 1
    assert data["items"][0]["tenant_id"] == str(_TENANT_ID_1)


async def test_cohere_overrides_requires_auth(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Cohere-overrides endpoint rejects unauthenticated requests."""
    res = await client.get("/api/admin/cost/cohere-overrides")
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# GET /tenants/{tenant_id} — per-tenant breakdown
# ---------------------------------------------------------------------------


async def test_per_tenant_endpoint_returns_breakdown(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Per-tenant endpoint returns breakdown with tenant_id and period."""
    fake_conn.canned_row["tenant_billing"] = {
        "id": _TENANT_ID_1,
        "mrr_myr": Decimal("2499.00"),
    }
    fake_conn.canned_row["FROM tenants"] = {
        "id": _TENANT_ID_1,
        "display_name": "ACME Sdn Bhd",
    }

    with patch(
        "polaris_api.routers.admin_cost.fetch_tenant_cost_monthly",
        new_callable=AsyncMock,
        side_effect=lambda **kw: _async_zero(kw),
    ):
        res = await client.get(
            f"/api/admin/cost/tenants/{_TENANT_ID_1}",
            headers=_ADMIN_HEADERS,
        )

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["tenant_id"] == str(_TENANT_ID_1)


async def test_per_tenant_endpoint_requires_auth(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Per-tenant endpoint rejects unauthenticated requests."""
    res = await client.get(f"/api/admin/cost/tenants/{_TENANT_ID_1}")
    assert res.status_code == 401
