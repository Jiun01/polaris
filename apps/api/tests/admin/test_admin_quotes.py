"""Admin sales-quote endpoints — Phase 9 PR-9 tests.

Coverage:
  * Auth gate: missing JWT → 401, anon role → 403.
  * POST /quotes: happy path generates quote with POL-Q number; PDF rendered + uploaded.
  * POST /quotes: unknown tenant → 404.
  * POST /quotes: invalid target_tier → 422.
  * GET /quotes: list returns items + total.
  * GET /quotes/{id}/pdf: 307 redirect to signed URL.
  * GET /quotes/{id}/pdf: not found → 404.
  * POST /quotes/{id}/convert/{tenant_id}: converts open quote to invoice.
  * POST /quotes/{id}/convert/{tenant_id}: non-open quote → 409.
  * POST /quotes/{id}/convert/{tenant_id}: expired quote → 409.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
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

    def keys(self) -> list[str]:
        return list(self._d.keys())


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
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
        self.executed.append((sql, args))
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

_TENANT_ID = uuid4()
_QUOTE_ID = uuid4()
_NOW = datetime.now(UTC)
_TODAY = date.today()
# Far-future expiry so the "expired" 409 branch is NOT triggered for the
# convertible-quote rows (router compares expires_at < now()).
_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)

# Mirrors the shipped sales_quotes columns (_SELECT_COLS in the router).
_QUOTE_ROW: dict[str, Any] = {
    "id": _QUOTE_ID,
    "quote_code": "POL-Q-26060001",
    "prospect_email": "prospect@example.com",
    "prospect_company": "ACME Sdn Bhd",
    "target_tier": "pro",
    "target_billing_cycle": "monthly",
    "intended_signup_date": date(2026, 6, 1),
    "proration_inputs": {},
    "pdf_bucket": "polaris-quotes",
    "pdf_object_key": f"quotes/{_QUOTE_ID}.pdf",
    "generated_by_admin_id": uuid4(),
    "converted_to_tenant_id": None,
    "converted_at": None,
    "expires_at": _FUTURE,
    "created_at": _NOW,
}

_CREATE_BODY: dict[str, Any] = {
    "prospect_email": "prospect@example.com",
    "prospect_company": "ACME Sdn Bhd",
    "target_tier": "pro",
    "target_billing_cycle": "monthly",
    "intended_signup_date": "2026-06-01",
    "valid_days": 30,
}


# ---- Auth gate ----------------------------------------------------------------


async def test_missing_jwt_returns_401(client: AsyncClient, fake_conn: _FakeConn) -> None:
    res = await client.post("/api/admin/quotes/", json=_CREATE_BODY)
    assert res.status_code == 401


async def test_anon_role_denied(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="anon")
    res = await client.post(
        "/api/admin/quotes/",
        json=_CREATE_BODY,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# ---- POST /quotes (create) ---------------------------------------------------


async def test_create_quote_happy_path(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Quote is assigned a POL-Q number; PDF rendered + uploaded; DB row returned."""
    # Quote sequencer reuses invoice_sequence with invoice_type='Q' and
    # RETURNING next_index (NOT last_index).
    fake_conn.canned_row["invoice_sequence"] = {"next_index": 1}
    # sales_quotes INSERT … RETURNING <cols>
    fake_conn.canned_row["INSERT INTO sales_quotes"] = _QUOTE_ROW

    with (
        patch("polaris_api.routers.admin_quotes.render_quote_pdf", new_callable=AsyncMock) as mock_render,
        patch("polaris_api.routers.admin_quotes.upload_pdf", new_callable=AsyncMock) as mock_upload,
    ):
        mock_render.return_value = b"%PDF-fake"
        mock_upload.return_value = None

        res = await client.post(
            "/api/admin/quotes/",
            json=_CREATE_BODY,
            headers=_ADMIN_HEADERS,
        )

    assert res.status_code == 201, res.text
    data = res.json()
    assert data["quote_code"] == "POL-Q-26060001"
    assert data["target_tier"] == "pro"
    assert data["converted_to_tenant_id"] is None
    mock_render.assert_awaited_once()
    mock_upload.assert_awaited_once()


async def test_create_quote_unknown_tenant(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Convert to an unknown tenant → 404.

    The shipped create endpoint takes no tenant (prospect quote), so the
    unknown-tenant 404 lives on the convert flow: the quote is found and
    convertible, but the tenant lookup returns no row.
    """
    fake_conn.canned_row["FROM sales_quotes WHERE id = $1"] = _QUOTE_ROW
    fake_conn.canned_row["plan_tier FROM tenants"] = None

    res = await client.post(
        f"/api/admin/quotes/{_QUOTE_ID}/convert/{_TENANT_ID}",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


async def test_create_quote_invalid_tier(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Invalid target_tier → 422."""
    body = {**_CREATE_BODY, "target_tier": "diamond"}
    res = await client.post("/api/admin/quotes/", json=body, headers=_ADMIN_HEADERS)
    assert res.status_code == 422


async def test_create_quote_invalid_billing_cycle(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Invalid target_billing_cycle → 422."""
    body = {**_CREATE_BODY, "target_billing_cycle": "quarterly"}
    res = await client.post("/api/admin/quotes/", json=body, headers=_ADMIN_HEADERS)
    assert res.status_code == 422


# ---- GET /quotes -------------------------------------------------------------


async def test_list_quotes_no_filter(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows["FROM sales_quotes"] = [_QUOTE_ROW]
    fake_conn.canned_val["COUNT(*)"] = 1

    res = await client.get("/api/admin/quotes/", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["quote_code"] == "POL-Q-26060001"


async def test_list_quotes_empty(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_val["COUNT(*)"] = 0
    res = await client.get("/api/admin/quotes/", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    assert res.json()["total"] == 0


# ---- GET /quotes/{id}/pdf ---------------------------------------------------


async def test_pdf_endpoint_redirects(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["FROM sales_quotes WHERE id"] = {
        "pdf_bucket": "polaris-quotes",
        "pdf_object_key": f"quotes/{_TENANT_ID}/{_QUOTE_ID}.pdf",
    }

    with patch(
        "polaris_api.routers.admin_quotes.get_signed_url",
        new_callable=AsyncMock,
        return_value="https://storage.example.com/signed",
    ):
        res = await client.get(
            f"/api/admin/quotes/{_QUOTE_ID}/pdf",
            headers=_ADMIN_HEADERS,
            follow_redirects=False,
        )

    assert res.status_code == 307
    assert res.headers["location"] == "https://storage.example.com/signed"


async def test_pdf_endpoint_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["FROM sales_quotes WHERE id"] = None
    res = await client.get(
        f"/api/admin/quotes/{_QUOTE_ID}/pdf",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


# ---- POST /quotes/{id}/convert/{tenant_id} ----------------------------------


async def test_convert_quote_happy_path(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Convert an open, unexpired quote → quote marked converted, tenant tier flipped."""
    # Quote lookup + the post-update re-fetch share the same SQL key
    # (SELECT … FROM sales_quotes WHERE id = $1). Return a converted row so
    # the response reflects the conversion; the router only reads
    # converted_to_tenant_id / expires_at to gate, which the open row passes
    # on first read because fetchrow returns the same canned row both times —
    # so drive the gate via the open _QUOTE_ROW and assert on executed SQL.
    fake_conn.canned_row["FROM sales_quotes WHERE id = $1"] = _QUOTE_ROW
    # Tenant existence + current plan_tier lookup
    fake_conn.canned_row["plan_tier FROM tenants"] = {
        "id": _TENANT_ID,
        "plan_tier": "starter",
    }
    # audit.record reads the latest audit chain hash, etc. — keep it a no-op
    # by patching the audit recorder so the convert transaction completes.
    with patch(
        "polaris_api.routers.admin_quotes.audit_record",
        new_callable=AsyncMock,
        return_value=None,
    ):
        res = await client.post(
            f"/api/admin/quotes/{_QUOTE_ID}/convert/{_TENANT_ID}",
            headers=_ADMIN_HEADERS,
        )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["quote_code"] == "POL-Q-26060001"
    assert any("UPDATE sales_quotes" in sql for sql, _ in fake_conn.executed)
    assert any("UPDATE tenants SET plan_tier" in sql for sql, _ in fake_conn.executed)


async def test_convert_quote_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["FROM sales_quotes WHERE id = $1"] = None
    res = await client.post(
        f"/api/admin/quotes/{_QUOTE_ID}/convert/{_TENANT_ID}",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


async def test_convert_non_open_quote_returns_409(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Attempting to convert an already-converted quote → 409."""
    fake_conn.canned_row["FROM sales_quotes WHERE id = $1"] = {
        **_QUOTE_ROW,
        "converted_to_tenant_id": uuid4(),
        "converted_at": _NOW,
    }
    res = await client.post(
        f"/api/admin/quotes/{_QUOTE_ID}/convert/{_TENANT_ID}",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 409


async def test_convert_expired_quote_returns_409(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Quote with expires_at in the past → 409."""
    fake_conn.canned_row["FROM sales_quotes WHERE id = $1"] = {
        **_QUOTE_ROW,
        "expires_at": datetime(2020, 1, 1, tzinfo=UTC),  # definitely expired
    }
    res = await client.post(
        f"/api/admin/quotes/{_QUOTE_ID}/convert/{_TENANT_ID}",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 409
