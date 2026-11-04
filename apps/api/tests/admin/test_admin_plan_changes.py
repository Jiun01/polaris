"""Admin plan-change endpoints — Phase 9 PR-8 tests.

Coverage:
  * Auth gate: missing JWT → 401, anon role → 403.
  * GET /plan-changes: list defaults to status=pending.
  * GET /plan-changes/{id}: fetch one request.
  * GET /plan-changes/{id}: unknown id → 404.
  * POST /plan-changes/{id}/approve: happy path creates invoice + returns amount_due.
  * POST /plan-changes/{id}/approve: idempotent (existing invoice → returns early).
  * POST /plan-changes/{id}/approve: non-pending status → 409.
  * POST /plan-changes/{id}/reject: happy path writes decision_note.
  * POST /plan-changes/{id}/reject: non-pending status → 409.
  * POST /plan-changes/{id}/reject: missing decision_note → 422.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
_REQUEST_ID = uuid4()
_ADMIN_ID = uuid4()
_INVOICE_ID = uuid4()
_NOW = datetime.now(UTC)

_PCR_ROW: dict[str, Any] = {
    "id": _REQUEST_ID,
    "tenant_id": _TENANT_ID,
    "tenant_name": "ACME Sdn Bhd",
    "requested_by_user_id": uuid4(),
    "current_tier": "starter",
    "target_tier": "pro",
    "current_billing_cycle": "monthly",
    "target_billing_cycle": "monthly",
    "proration_preview": json.dumps({"amount_due_myr": "1400.00"}),
    "status": "pending",
    "reason": "upgrade request from customer",
    "decided_by_user_id": None,
    "decided_at": None,
    "decision_note": None,
    "created_at": _NOW,
}


# ---- Auth gate ----------------------------------------------------------------


async def test_missing_jwt_returns_401(client: AsyncClient, fake_conn: _FakeConn) -> None:
    res = await client.get("/api/admin/plan-changes/")
    assert res.status_code == 401


async def test_anon_role_denied(client: AsyncClient, fake_conn: _FakeConn) -> None:
    token = _mint_jwt(role="anon")
    res = await client.get(
        "/api/admin/plan-changes/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# ---- GET /plan-changes -------------------------------------------------------


async def test_list_plan_changes_defaults_to_pending(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_rows["FROM plan_change_requests"] = [_PCR_ROW]
    fake_conn.canned_val["COUNT(*)"] = 1

    res = await client.get("/api/admin/plan-changes/", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["status"] == "pending"


async def test_list_plan_changes_empty(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_val["COUNT(*)"] = 0
    res = await client.get("/api/admin/plan-changes/", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    assert res.json()["total"] == 0
    assert res.json()["items"] == []


# ---- GET /plan-changes/{id} --------------------------------------------------


async def test_get_plan_change_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["WHERE pcr.id"] = _PCR_ROW
    res = await client.get(f"/api/admin/plan-changes/{_REQUEST_ID}", headers=_ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["current_tier"] == "starter"
    assert data["target_tier"] == "pro"


async def test_get_plan_change_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["WHERE pcr.id"] = None
    res = await client.get(f"/api/admin/plan-changes/{_REQUEST_ID}", headers=_ADMIN_HEADERS)
    assert res.status_code == 404


# ---- POST /plan-changes/{id}/approve ----------------------------------------


async def test_approve_plan_change_happy_path(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Approve creates an invoice + flips request status."""
    fake_conn.canned_row["WHERE pcr.id"] = _PCR_ROW
    # No existing invoice → proceed with approval
    fake_conn.canned_row["proration_basis->>'plan_change_request_id'"] = None
    # invoice INSERT RETURNING (router SELECTs amount_myr, not total_myr)
    fake_conn.canned_row["RETURNING id, invoice_number, amount_myr"] = {
        "id": _INVOICE_ID,
        "invoice_number": "POL-M-26060001",
        "amount_myr": "950.00",
    }
    # invoice number sequencer (RETURNING next_index)
    fake_conn.canned_row["invoice_sequence"] = {"next_index": 1}

    with (
        patch("polaris_api.routers.admin_plan_changes.render_invoice_pdf", new_callable=AsyncMock) as mock_render,
        patch("polaris_api.routers.admin_plan_changes.upload_pdf", new_callable=AsyncMock) as mock_upload,
    ):
        mock_render.return_value = b"%PDF-fake"
        mock_upload.return_value = None

        res = await client.post(
            f"/api/admin/plan-changes/{_REQUEST_ID}/approve",
            headers=_ADMIN_HEADERS,
        )

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "approved"
    assert data["idempotent"] is False
    mock_render.assert_awaited_once()
    mock_upload.assert_awaited_once()


async def test_approve_plan_change_idempotent(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Second approval call returns the existing invoice without creating a new one."""
    fake_conn.canned_row["WHERE pcr.id"] = _PCR_ROW
    fake_conn.canned_row["proration_basis->>'plan_change_request_id'"] = {
        "id": _INVOICE_ID,
        "invoice_number": "POL-M-26060001",
        "amount_myr": "950.00",
    }

    with (
        patch("polaris_api.routers.admin_plan_changes.render_invoice_pdf", new_callable=AsyncMock) as mock_render,
        patch("polaris_api.routers.admin_plan_changes.upload_pdf", new_callable=AsyncMock) as mock_upload,
    ):
        res = await client.post(
            f"/api/admin/plan-changes/{_REQUEST_ID}/approve",
            headers=_ADMIN_HEADERS,
        )

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "approved"
    assert data["idempotent"] is True
    mock_render.assert_not_awaited()
    mock_upload.assert_not_awaited()


async def test_approve_plan_change_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["WHERE pcr.id"] = None
    res = await client.post(
        f"/api/admin/plan-changes/{_REQUEST_ID}/approve",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


async def test_approve_non_pending_returns_409(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["WHERE pcr.id"] = {**_PCR_ROW, "status": "approved"}
    res = await client.post(
        f"/api/admin/plan-changes/{_REQUEST_ID}/approve",
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 409


# ---- POST /plan-changes/{id}/reject -----------------------------------------


async def test_reject_plan_change_happy_path(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Reject writes decision_note and returns rejected status."""
    fake_conn.canned_row["SELECT id, tenant_id, status FROM plan_change_requests"] = {
        "id": _REQUEST_ID,
        "tenant_id": _TENANT_ID,
        "status": "pending",
    }

    res = await client.post(
        f"/api/admin/plan-changes/{_REQUEST_ID}/reject",
        json={"decision_note": "Customer changed their mind."},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "rejected"
    assert data["decided_at"] is not None
    assert any("UPDATE plan_change_requests" in sql for sql, _ in fake_conn.executed)


async def test_reject_plan_change_not_found(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["SELECT id, tenant_id, status FROM plan_change_requests"] = None
    res = await client.post(
        f"/api/admin/plan-changes/{_REQUEST_ID}/reject",
        json={"decision_note": "reason"},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 404


async def test_reject_non_pending_returns_409(client: AsyncClient, fake_conn: _FakeConn) -> None:
    fake_conn.canned_row["SELECT id, tenant_id, status FROM plan_change_requests"] = {
        "id": _REQUEST_ID,
        "tenant_id": _TENANT_ID,
        "status": "rejected",
    }
    res = await client.post(
        f"/api/admin/plan-changes/{_REQUEST_ID}/reject",
        json={"decision_note": "reason"},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 409


async def test_reject_missing_decision_note_returns_422(client: AsyncClient, fake_conn: _FakeConn) -> None:
    res = await client.post(
        f"/api/admin/plan-changes/{_REQUEST_ID}/reject",
        json={},
        headers=_ADMIN_HEADERS,
    )
    assert res.status_code == 422
