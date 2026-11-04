"""Admin billing endpoints — Phase 9 PR-7 tests.

Coverage:
  * Auth gate: missing JWT → 401, anon role → 403.
  * POST /invoices: happy path creates invoice with POL number; PDF rendered + uploaded.
  * POST /invoices: unknown tenant → 404.
  * POST /invoices/{id}/mark-paid: flips status + records paid_at.
  * POST /invoices/{id}/mark-paid: already-paid is idempotent (200).
  * GET /invoices: list + status filter works.
  * GET /invoices/{invoice_id}/pdf: 307 redirect to signed URL.
  * GET /archive: returns aggregates.
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
_INVOICE_ID = uuid4()
_ADMIN_ID = uuid4()

_NOW = datetime.now(UTC)

_INVOICE_ROW: dict[str, Any] = {
    "id": _INVOICE_ID,
    "tenant_id": _TENANT_ID,
    "invoice_number": "POL-M-2606-0001",
    "invoice_kind": "monthly_subscription",
    "period": "2026-06",
    "period_start": date(2026, 6, 1),
    "period_end": date(2026, 6, 30),
    "subtotal_myr": 2499.0,
    "tax_myr": 0.0,
    "total_myr": 2499.0,
    "amount_myr": 2499.0,
    "status": "issued",
    "issued_at": _NOW,
    "due_at": _NOW,
    "paid_at": None,
    "marked_paid_by_admin_id": None,
    "void_reason": None,
    "pdf_bucket": "polaris-invoices",
    "pdf_object_key": f"invoices/{_TENANT_ID}/{_INVOICE_ID}.pdf",
    "created_at": _NOW,
    "updated_at": _NOW,
}

_ISSUE_BODY: dict[str, Any] = {
    "tenant_id": str(_TENANT_ID),
    "invoice_kind": "monthly_subscription",
    "period_start": "2026-06-01",
    "period_end": "2026-06-30",
    "subtotal_myr": 2499.0,
    "tax_myr": 0.0,
    "due_at": "2026-07-14T00:00:00Z",
}


# ---- Auth gate ----------------------------------------------------------------


async def test_missing_jwt_returns_401(client: AsyncClient, fake_conn: _FakeConn) -> None:
    res = await client.post("/api/admin/billing/invoices", json=_ISSUE_BODY)
    assert res.status_code == 401


async def test_anon_role_denied(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="anon")
    res = await client.post(
        "/api/admin/billing/invoices",
        json=_ISSUE_BODY,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# ---- POST /invoices (issue) --------------------------------------------------


async def test_issue_invoice_happy_path(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Invoice is assigned a POL number; PDF rendered + uploaded; DB row returned."""
    # Tenant lookup
    fake_conn.canned_row["display_name"] = {"id": _TENANT_ID, "display_name": "ACME Sdn Bhd"}
    # invoice_sequence INSERT RETURNING
    fake_conn.canned_row["invoice_sequence"] = {"next_index": 1}
    # tenant_invoices INSERT RETURNING
    fake_conn.canned_row["INSERT INTO tenant_invoices"] = _INVOICE_ROW

    with (
        patch("polaris_api.routers.admin_billing.render_invoice_pdf", new_callable=AsyncMock) as mock_render,
        patch("polaris_api.routers.admin_billing.upload_pdf", new_callable=AsyncMock) as mock_upload,
    ):
        mock_render.return_value = b"%PDF-fake"
        mock_upload.return_value = None

        res = await client.post(
            "/api/admin/billing/invoices",
            json=_ISSUE_BODY,
            headers=_ADMIN_HEADERS,
        )

    assert res.status_code == 201, res.text
    data = res.json()
    assert data["invoice_number"] == "POL-M-2606-0001"
    assert data["status"] == "issued"
    mock_render.assert_awaited_once()
    mock_upload.assert_awaited_once()


async def test_issue_invoice_unknown_tenant(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Unknown tenant → 404."""
    # tenant lookup returns nothing
    fake_conn.canned_row["display_name"] = None

    res = await client.post(
        "/api/admin/billing/invoices",
        json=_ISSUE_BODY,
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


async def test_issue_invoice_invalid_kind(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Invalid invoice_kind → 422."""
    body = {**_ISSUE_BODY, "invoice_kind": "not_a_kind"}
    res = await client.post("/api/admin/billing/invoices", json=body, headers=_ADMIN_HEADERS)
    assert res.status_code == 422


# ---- POST /invoices/{id}/mark-paid ------------------------------------------


async def test_mark_paid_flips_status(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """mark-paid flips status='paid' and records paid_at."""
    fake_conn.canned_row["SELECT id, tenant_id, invoice_number, status"] = {
        **_INVOICE_ROW,
        "status": "issued",
    }

    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/mark-paid",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "paid"
    assert data["paid_at"] is not None
    # UPDATE was executed
    assert any("UPDATE tenant_invoices" in sql for sql, _ in fake_conn.executed)


async def test_mark_paid_already_paid_is_idempotent(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Calling mark-paid on an already-paid invoice returns 200 without duplicate UPDATE."""
    paid_at = _NOW
    fake_conn.canned_row["SELECT id, tenant_id, invoice_number, status"] = {
        **_INVOICE_ROW,
        "status": "paid",
        "paid_at": paid_at,
        "marked_paid_by_admin_id": _ADMIN_ID,
    }

    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/mark-paid",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "paid"
    # No UPDATE should have been executed (idempotent)
    assert not any("UPDATE tenant_invoices" in sql for sql, _ in fake_conn.executed)


async def test_mark_paid_invoice_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["SELECT id, tenant_id, invoice_number, status"] = None
    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/mark-paid",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


# ---- POST /invoices/{id}/void -----------------------------------------------


async def test_void_flips_status(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """void flips status='void' and records the reason."""
    fake_conn.canned_row["SELECT id, tenant_id, invoice_number, status"] = {
        **_INVOICE_ROW,
        "status": "issued",
    }
    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/void",
        json={"void_reason": "duplicate charge"},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "void"
    assert data["void_reason"] == "duplicate charge"
    assert any("UPDATE tenant_invoices" in sql for sql, _ in fake_conn.executed)


async def test_void_requires_reason(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """An empty reason is rejected by validation (422)."""
    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/void",
        json={"void_reason": ""},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 422


async def test_void_already_void_is_idempotent(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["SELECT id, tenant_id, invoice_number, status"] = {
        **_INVOICE_ROW,
        "status": "void",
        "void_reason": "original reason",
    }
    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/void",
        json={"void_reason": "new reason"},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "void"
    assert data["void_reason"] == "original reason"
    assert not any("UPDATE tenant_invoices" in sql for sql, _ in fake_conn.executed)


async def test_void_paid_invoice_conflict(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["SELECT id, tenant_id, invoice_number, status"] = {
        **_INVOICE_ROW,
        "status": "paid",
    }
    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/void",
        json={"void_reason": "oops"},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 409


async def test_void_invoice_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["SELECT id, tenant_id, invoice_number, status"] = None
    res = await client.post(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/void",
        json={"void_reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


# ---- GET /invoices -----------------------------------------------------------


async def test_list_invoices_no_filter(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows["FROM tenant_invoices"] = [_INVOICE_ROW]
    fake_conn.canned_val["COUNT(*)"] = 1

    res = await client.get("/api/admin/billing/invoices", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1


async def test_list_invoices_invalid_status_returns_422(client: AsyncClient, fake_conn: _FakeConn) -> None:
    res = await client.get(
        "/api/admin/billing/invoices?status=unknown_status",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 422


async def test_list_invoices_returns_only_allowlisted_columns(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """No i.* — response items must not leak columns outside _TenantInvoice
    (e.g. proration_basis), and every allowlisted field must be populated."""
    fake_conn.canned_rows["FROM tenant_invoices"] = [
        {**_INVOICE_ROW, "tenant_name": "ACME Sdn Bhd", "proration_basis": {"leak": "me"}}
    ]
    fake_conn.canned_val["COUNT(*)"] = 1

    res = await client.get("/api/admin/billing/invoices", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    item = res.json()["items"][0]
    assert "proration_basis" not in item
    assert item["invoice_number"] == _INVOICE_ROW["invoice_number"]
    assert item["tenant_name"] == "ACME Sdn Bhd"


async def test_list_invoices_cursor_round_trip(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """A next_cursor from page 1 is accepted as the cursor for page 2."""
    fake_conn.canned_row["SELECT issued_at, id FROM tenant_invoices"] = {
        "issued_at": _INVOICE_ROW["issued_at"],
        "id": _INVOICE_ID,
    }
    fake_conn.canned_rows["FROM tenant_invoices"] = [_INVOICE_ROW]
    fake_conn.canned_val["COUNT(*)"] = 1

    res = await client.get(
        f"/api/admin/billing/invoices?cursor={_INVOICE_ID}",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1


async def test_list_invoices_invalid_cursor_returns_422(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    fake_conn.canned_row["SELECT issued_at, id FROM tenant_invoices"] = None

    res = await client.get(
        f"/api/admin/billing/invoices?cursor={uuid4()}",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 422


async def test_list_invoices_zero_param_backward_compat(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Zero-param call (the current BillingPage caller) still returns a valid
    {items, total} shape for <=50 rows, with next_cursor null (no more pages)."""
    fake_conn.canned_rows["FROM tenant_invoices"] = [_INVOICE_ROW]
    fake_conn.canned_val["COUNT(*)"] = 1

    res = await client.get("/api/admin/billing/invoices", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["next_cursor"] is None


# ---- GET /invoices/{id}/pdf -------------------------------------------------


async def test_pdf_endpoint_redirects(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["pdf_bucket, pdf_object_key"] = {
        "pdf_bucket": "polaris-invoices",
        "pdf_object_key": f"invoices/{_TENANT_ID}/{_INVOICE_ID}.pdf",
    }

    with patch(
        "polaris_api.routers.admin_billing.get_signed_url",
        new_callable=AsyncMock,
        return_value="https://storage.example.com/signed",
    ):
        res = await client.get(
            f"/api/admin/billing/invoices/{_INVOICE_ID}/pdf",
            headers=_ADMIN_HEADERS,
            follow_redirects=False,
        )

    assert res.status_code == 307
    assert res.headers["location"] == "https://storage.example.com/signed"


async def test_pdf_endpoint_invoice_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["pdf_bucket, pdf_object_key"] = None
    res = await client.get(
        f"/api/admin/billing/invoices/{_INVOICE_ID}/pdf",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


# ---- GET /archive -----------------------------------------------------------


async def test_get_archive_returns_aggregates(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows["GROUP BY year_month"] = [
        {"year_month": "2026-06", "tenant_id": _TENANT_ID, "count": 3},
    ]

    res = await client.get("/api/admin/billing/archive", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["year_month"] == "2026-06"
    assert data["items"][0]["count"] == 3
