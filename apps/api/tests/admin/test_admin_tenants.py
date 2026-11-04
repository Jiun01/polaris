"""Admin tenant-channels endpoint tests — Phase 10 PR-0-retrofit.

Coverage (per checklist §PR-0-retrofit):
  (a) override=True  Starter + Instagram → 201 + channel row + audit action
      'channels_per_kind.override' with non-empty reason
  (b) override=True  blank reason          → 422 (Pydantic validation)
  (c) override=False Starter + Instagram   → 403 (tier-cap rejected)
  (d) override=True  Pro + 2nd WhatsApp    → 201 + audit row (multiplicity bypass)

Auth gate:
  - missing JWT                                   → 401
  - impersonation header present                  → 403
  - wrong role (tenant_admin)                     → 403

Security invariants verified:
  - audit row written for every successful channel add
  - no path sets override_tier_cap=True without a non-empty reason
  - impersonation header always blocked
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

# ---------------------------------------------------------------- helpers ----


def _mint_admin_jwt(*, sub: UUID | None = None) -> str:
    now = int(time.time())
    admin_id = sub or uuid4()
    payload: dict[str, Any] = {
        "sub": str(admin_id),
        "platform_admin_id": str(admin_id),
        "role": "platform_admin",
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


def _mint_tenant_jwt() -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "role": "admin",  # tenant_admin role
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
        return self._d[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._d


class _FakeConn:
    """Minimal asyncpg.Connection stand-in that records executed SQL."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        # Key = fragment to match in SQL; value = response.
        self._row_map: dict[str, dict[str, Any] | None] = {}
        self._rows_map: dict[str, list[dict[str, Any]]] = {}
        self._val_map: dict[str, Any] = {}

    # --- setup helpers -------------------------------------------------------

    def canned_row(self, sql_fragment: str, row: dict[str, Any] | None) -> None:
        self._row_map[sql_fragment] = row

    def canned_rows(self, sql_fragment: str, rows: list[dict[str, Any]]) -> None:
        self._rows_map[sql_fragment] = rows

    def canned_val(self, sql_fragment: str, val: Any) -> None:
        self._val_map[sql_fragment] = val

    # --- asyncpg interface ---------------------------------------------------

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRecord | None:
        for frag, row in self._row_map.items():
            if frag in sql:
                return _FakeRecord(row) if row is not None else None
        return None

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
        for frag, rows in self._rows_map.items():
            if frag in sql:
                return [_FakeRecord(r) for r in rows]
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        for frag, val in self._val_map.items():
            if frag in sql:
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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _channel_body(
    *,
    channel_kind: str = "messenger",
    chatwoot_inbox_id: int = 42,
    override_tier_cap: bool = False,
    override_reason: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "channel_kind": channel_kind,
        "connection_payload": {
            "chatwoot_inbox_id": chatwoot_inbox_id,
            "ig_business_account_id": "ig-biz-001",
        },
        "override_tier_cap": override_tier_cap,
    }
    if override_reason is not None:
        body["override_reason"] = override_reason
    return body


# ---------------------------------------------------------------- auth gate ----


async def test_missing_jwt_returns_401(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    tenant_id = uuid4()
    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(),
    )
    assert res.status_code == 401


async def test_tenant_admin_jwt_returns_403(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    tenant_id = uuid4()
    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(),
        headers={"Authorization": f"Bearer {_mint_tenant_jwt()}"},
    )
    assert res.status_code == 403


async def test_impersonation_header_returns_403(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Security invariant: override never reachable during impersonation."""
    tenant_id = uuid4()
    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(override_tier_cap=True, override_reason="some reason"),
        headers={
            "Authorization": f"Bearer {_mint_admin_jwt()}",
            "X-Polaris-Impersonating": "some-session-id",
        },
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "forbidden_during_impersonation"


# ---- (b) override=True + blank reason → 422 --------------------------------


async def test_override_blank_reason_422(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Pydantic validation catches blank override_reason before the DB."""
    tenant_id = uuid4()
    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json={
            "channel_kind": "messenger",
            "connection_payload": {"chatwoot_inbox_id": 42},
            "override_tier_cap": True,
            # override_reason intentionally absent (empty)
        },
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 422


async def test_override_empty_string_reason_422(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    tenant_id = uuid4()
    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json={
            "channel_kind": "messenger",
            "connection_payload": {"chatwoot_inbox_id": 42},
            "override_tier_cap": True,
            "override_reason": "   ",  # whitespace-only = blank
        },
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 422


# ---- (c) override=False Starter + Instagram → 403 --------------------------


async def test_no_override_starter_instagram_403(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Starter allowed_channel_kinds = ['whatsapp']; instagram → 403."""
    tenant_id = uuid4()

    fake_conn.canned_row(
        "FROM tenants",
        {"id": tenant_id, "status": "active", "chatwoot_account_id": None},
    )
    # plan_tier fetchval
    fake_conn.canned_val("plan_tier FROM tenant_quotas", "starter")
    # tenant_channels COUNT for before-state
    fake_conn.canned_val("SELECT COUNT(*) FROM tenant_channels", 0)
    # provision service: tenant_quotas read
    fake_conn.canned_row(
        "allowed_channel_kinds",
        {
            "plan_tier": "starter",
            "allowed_channel_kinds": ["whatsapp"],
        },
    )

    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(
            channel_kind="messenger",
            override_tier_cap=False,
        ),
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 403
    assert "messenger" in res.json()["detail"].lower()


# ---- (a) override=True Starter + Instagram → 201 + audit row ---------------


async def test_override_true_starter_instagram_201(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Platform_admin bypasses tier cap; channel added; audit row written."""
    tenant_id = uuid4()
    new_channel_id = uuid4()

    fake_conn.canned_row(
        "FROM tenants",
        {"id": tenant_id, "status": "active", "chatwoot_account_id": None},
    )
    fake_conn.canned_val("plan_tier FROM tenant_quotas", "starter")
    fake_conn.canned_val("SELECT COUNT(*) FROM tenant_channels", 0)
    # provision INSERT RETURNING id
    fake_conn.canned_val("INSERT INTO tenant_channels", new_channel_id)
    # audit_record advisory lock + prev_hash fetchval → use a fragment that matches
    fake_conn.canned_val("hashtext", None)
    fake_conn.canned_val("audit_log_prev_hash", None)

    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(
            channel_kind="messenger",
            override_tier_cap=True,
            override_reason="Sales exception for ACME Co onboarding",
        ),
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["channel_kind"] == "messenger"
    assert body["override_applied"] is True

    # Verify an audit execute call was made with the override action
    audit_inserts = [
        sql for sql, _ in fake_conn.executed
        if "INSERT INTO audit_logs" in sql
    ]
    assert len(audit_inserts) >= 1


# ---- (d) override=True Pro + 2nd WhatsApp → 201 (multiplicity bypass) ------


async def test_override_true_pro_second_whatsapp_201(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Pro tier + WhatsApp is allowed; override bypasses any additional cap."""
    tenant_id = uuid4()
    new_channel_id = uuid4()

    fake_conn.canned_row(
        "FROM tenants",
        {"id": tenant_id, "status": "active", "chatwoot_account_id": None},
    )
    fake_conn.canned_val("plan_tier FROM tenant_quotas", "pro")
    # already has 1 WhatsApp inbox
    fake_conn.canned_val("SELECT COUNT(*) FROM tenant_channels", 1)
    fake_conn.canned_val("INSERT INTO tenant_channels", new_channel_id)
    fake_conn.canned_val("hashtext", None)
    fake_conn.canned_val("audit_log_prev_hash", None)

    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(
            channel_kind="whatsapp",
            chatwoot_inbox_id=99,
            override_tier_cap=True,
            override_reason="Second WABA for ACME SG entity",
        ),
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["channel_kind"] == "whatsapp"
    assert body["override_applied"] is True

    audit_inserts = [
        sql for sql, _ in fake_conn.executed
        if "INSERT INTO audit_logs" in sql
    ]
    assert len(audit_inserts) >= 1


# ---- email → 201 (email is provisionable; asymmetry fix) ------------------


async def test_pro_email_channel_added_201(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Email IS provisionable via add_channel (closes the admin asymmetry where
    email could be entitled at create but never added afterward). Uses the
    override path — bypasses tier-cap but still enforces _PROVISIONABLE_KINDS, so
    a 201 proves 'email' is now a provisionable kind. chatwoot_account_id=None
    skips the best-effort agent-bot attach (no Chatwoot client built in tests)."""
    tenant_id = uuid4()
    new_channel_id = uuid4()

    fake_conn.canned_row(
        "FROM tenants",
        {"id": tenant_id, "status": "active", "chatwoot_account_id": None},
    )
    fake_conn.canned_val("plan_tier FROM tenant_quotas", "pro")
    fake_conn.canned_val("SELECT COUNT(*) FROM tenant_channels", 0)
    fake_conn.canned_val("INSERT INTO tenant_channels", new_channel_id)
    fake_conn.canned_val("hashtext", None)
    fake_conn.canned_val("audit_log_prev_hash", None)

    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(
            channel_kind="email",
            chatwoot_inbox_id=6,
            override_tier_cap=True,
            override_reason="Enable email drafting for ACME Co",
        ),
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["channel_kind"] == "email"
    # Email is Chatwoot-native — no outbound token is ever captured.
    assert body.get("token_captured") in (None, False)


async def test_pro_email_attaches_agent_bot(
    client: AsyncClient, fake_conn: _FakeConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding email attaches the Polaris Bot to the inbox when the tenant has a
    Chatwoot account id — the exact wiring whose absence left the email inbox
    webhook-silent (no draft). Asserts the attach call, not just the 201."""
    from unittest.mock import AsyncMock

    tenant_id = uuid4()
    new_channel_id = uuid4()
    account_id = 7

    fake_conn.canned_row(
        "FROM tenants",
        {"id": tenant_id, "status": "active", "chatwoot_account_id": account_id},
    )
    fake_conn.canned_val("plan_tier FROM tenant_quotas", "pro")
    fake_conn.canned_val("SELECT COUNT(*) FROM tenant_channels", 0)
    fake_conn.canned_val("INSERT INTO tenant_channels", new_channel_id)
    fake_conn.canned_val("hashtext", None)
    fake_conn.canned_val("audit_log_prev_hash", None)

    attach = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client", lambda: object()
    )
    monkeypatch.setattr(
        "polaris_api.routers.admin_tenant_channels.attach_inbox_agent_bot", attach
    )

    res = await client.post(
        f"/api/admin/tenants/{tenant_id}/channels",
        json=_channel_body(
            channel_kind="email",
            chatwoot_inbox_id=6,
            override_tier_cap=True,
            override_reason="Enable email drafting for ACME Co",
        ),
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 201
    attach.assert_awaited_once()
    assert attach.await_args.kwargs["account_id"] == account_id
    assert attach.await_args.kwargs["inbox_id"] == 6


# ---------------------------------------------------------- remove channel ----


async def test_remove_channel_200_deletes_row_and_secret(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """DELETE removes the channel row + its captured token; audit row written."""
    tenant_id = uuid4()
    deleted_id = uuid4()

    fake_conn.canned_row("FROM tenants", {"id": tenant_id})
    fake_conn.canned_val("DELETE FROM tenant_channels", deleted_id)
    # No sibling channel shares the token kind → secret IS removed.
    fake_conn.canned_val("SELECT COUNT(*) FROM tenant_channels", 0)
    fake_conn.canned_val("hashtext", None)
    fake_conn.canned_val("audit_log_prev_hash", None)

    res = await client.delete(
        f"/api/admin/tenants/{tenant_id}/channels/whatsapp",
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["channel_kind"] == "whatsapp"
    assert body["secret_removed"] == "meta_waba_access_token"

    assert any(
        "DELETE FROM tenant_secrets" in sql for sql, _ in fake_conn.executed
    )
    assert any(
        "INSERT INTO audit_logs" in sql for sql, _ in fake_conn.executed
    )


async def test_remove_channel_not_found_404(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """No channel row of that kind → deprovision raises → 404."""
    tenant_id = uuid4()
    fake_conn.canned_row("FROM tenants", {"id": tenant_id})
    fake_conn.canned_val("DELETE FROM tenant_channels", None)  # nothing deleted

    res = await client.delete(
        f"/api/admin/tenants/{tenant_id}/channels/whatsapp",
        headers={"Authorization": f"Bearer {_mint_admin_jwt()}"},
    )
    assert res.status_code == 404


async def test_remove_channel_impersonation_403(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Impersonation session cannot remove channels (same guard as add)."""
    tenant_id = uuid4()
    res = await client.delete(
        f"/api/admin/tenants/{tenant_id}/channels/whatsapp",
        headers={
            "Authorization": f"Bearer {_mint_admin_jwt()}",
            "X-Polaris-Impersonating": "1",
        },
    )
    assert res.status_code == 403
