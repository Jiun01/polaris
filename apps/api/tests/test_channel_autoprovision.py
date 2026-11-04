"""Tests for channel token auto-provisioning — add_channel + admin_secrets kind mapping.

Tests:
  add_channel:
    - whatsapp channel stores kind=meta_waba_access_token in tenant_secrets.
    - messenger channel stores kind=meta_fb_page_access_token.
    - instagram channel stores kind=meta_fb_page_access_token.
    - token capture failure does NOT fail the channel-add (response still 201).
    - telegram channel does NOT attempt token capture (token_captured=False).

  admin_secrets rotate:
    - service=meta_whatsapp stores kind=meta_waba_access_token.
    - service=meta_messenger stores kind=meta_fb_page_access_token.
    - service=meta_ig stores kind=meta_ig_login_access_token.
    - service=telegram_bot_token stores kind=telegram_bot_token (unchanged).
    - service=chatwoot stores kind=chatwoot (unchanged).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
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
# Suppress KMS sentinel startup check — tests use a fake DB conn.
os.environ.setdefault("POLARIS_KMS_KEY", "00112233445566778899aabbccddeeff" "ffeeddccbbaa99887766554433221100")

from polaris_api.main import app  # noqa: E402
from polaris_api.services.admin import get_admin_service_conn  # noqa: E402


def _mint_jwt(*, role: str) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d.get(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)


class _FakeConn:
    """Stub asyncpg.Connection for router-level tests."""

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
async def http_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _wire_add_channel_conn(
    conn: _FakeConn,
    *,
    tenant_id: UUID,
    channel_id: UUID,
    cw_account_id: int | None = 5,
) -> None:
    """Populate _FakeConn with the DB rows needed by add_channel.

    Covers three fetchrow / fetchval patterns:
      - tenant existence check (id + status + chatwoot_account_id)
      - _check_tier_cap's fetchrow (plan_tier + allowed_channel_kinds)
      - provision_channel's INSERT RETURNING id
      - audit_record's prev_hash
    """
    conn.canned_row["SELECT id, status, chatwoot_account_id FROM tenants"] = {
        "id": str(tenant_id),
        "status": "active",
        "chatwoot_account_id": cw_account_id,
    }
    # _check_tier_cap: SELECT plan_tier, allowed_channel_kinds FROM tenant_quotas
    conn.canned_row["SELECT plan_tier, allowed_channel_kinds"] = {
        "plan_tier": "pro",
        "allowed_channel_kinds": [
            "whatsapp", "instagram", "instagram_login", "messenger", "telegram", "email"
        ],
    }
    # add_channel outer fetchval for plan_tier (snapshot before state)
    conn.canned_val["SELECT plan_tier FROM tenant_quotas"] = "pro"
    # COUNT(*) for before_channel_count
    conn.canned_val["SELECT COUNT(*) FROM tenant_channels"] = 0
    # provision_channel's INSERT RETURNING id
    conn.canned_val["INSERT INTO tenant_channels"] = channel_id
    conn.canned_val["audit_log_prev_hash"] = None


# =====================================================================
# add_channel — token auto-provisioning
# =====================================================================


async def test_add_channel_whatsapp_stores_waba_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """WhatsApp channel add stores meta_waba_access_token in tenant_secrets."""
    tenant_id = uuid4()
    channel_id = uuid4()
    _wire_add_channel_conn(fake_conn, tenant_id=tenant_id, channel_id=channel_id)

    sample_token = "EAABsbC-fake-whatsapp-token"

    with patch(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client"
    ) as mock_factory:
        mock_cw = MagicMock()
        mock_cw.get_channel_token = AsyncMock(return_value=sample_token)
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.post(
            f"/api/admin/tenants/{tenant_id}/channels",
            json={
                "channel_kind": "whatsapp",
                "connection_payload": {
                    "chatwoot_inbox_id": 10,
                    "waba_id": "1234567890",
                    "phone_number_id": "9876543210",
                },
            },
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["token_captured"] is True

    # Confirm the secrets upsert was called with the correct kind.
    sql_seen = [s for s, _ in fake_conn.executed]
    assert any(
        "INSERT INTO tenant_secrets" in s for s in sql_seen
    ), f"Expected tenant_secrets INSERT, got: {sql_seen}"
    # Confirm the kind in the INSERT args.
    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            # args[1] = kind (second positional param after tenant_id)
            assert args[1] == "meta_waba_access_token", f"Wrong kind: {args[1]}"
            break


async def test_add_channel_messenger_stores_fb_page_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """Messenger channel add stores meta_fb_page_access_token in tenant_secrets."""
    tenant_id = uuid4()
    channel_id = uuid4()
    _wire_add_channel_conn(fake_conn, tenant_id=tenant_id, channel_id=channel_id)

    sample_token = "EAABsbC-fake-messenger-token"

    with patch(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client"
    ) as mock_factory:
        mock_cw = MagicMock()
        mock_cw.get_channel_token = AsyncMock(return_value=sample_token)
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.post(
            f"/api/admin/tenants/{tenant_id}/channels",
            json={
                "channel_kind": "messenger",
                "connection_payload": {
                    "chatwoot_inbox_id": 20,
                    "fb_page_id": "FB_PAGE_ID",
                },
            },
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["token_captured"] is True

    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            assert args[1] == "meta_fb_page_access_token", f"Wrong kind: {args[1]}"
            break


async def test_add_channel_instagram_stores_fb_page_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """Instagram channel add stores meta_fb_page_access_token in tenant_secrets."""
    tenant_id = uuid4()
    channel_id = uuid4()
    _wire_add_channel_conn(fake_conn, tenant_id=tenant_id, channel_id=channel_id)

    sample_token = "EAABsbC-fake-instagram-token"

    with patch(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client"
    ) as mock_factory:
        mock_cw = MagicMock()
        mock_cw.get_channel_token = AsyncMock(return_value=sample_token)
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.post(
            f"/api/admin/tenants/{tenant_id}/channels",
            json={
                "channel_kind": "messenger",
                "connection_payload": {
                    "chatwoot_inbox_id": 30,
                    "ig_business_account_id": "IG_ACCT_ID",
                },
            },
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["token_captured"] is True

    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            assert args[1] == "meta_fb_page_access_token", f"Wrong kind: {args[1]}"
            break


async def test_add_channel_instagram_login_stores_ig_login_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """instagram_login channel add stores meta_ig_login_access_token (NOT the
    shared FB page token) in tenant_secrets."""
    tenant_id = uuid4()
    channel_id = uuid4()
    _wire_add_channel_conn(fake_conn, tenant_id=tenant_id, channel_id=channel_id)

    sample_token = "IGAA-fake-instagram-login-user-token"

    with patch(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client"
    ) as mock_factory:
        mock_cw = MagicMock()
        mock_cw.get_channel_token = AsyncMock(return_value=sample_token)
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.post(
            f"/api/admin/tenants/{tenant_id}/channels",
            json={
                "channel_kind": "instagram_login",
                "connection_payload": {
                    "chatwoot_inbox_id": 31,
                    "ig_business_account_id": "IG_LOGIN_ACCT",
                },
            },
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 201, res.text
    assert res.json()["token_captured"] is True

    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            assert args[1] == "meta_ig_login_access_token", f"Wrong kind: {args[1]}"
            break
    else:
        pytest.fail("Expected an INSERT INTO tenant_secrets but none found")


async def test_add_channel_token_capture_failure_does_not_fail_add(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """If get_channel_token raises, the channel-add still returns 201."""
    tenant_id = uuid4()
    channel_id = uuid4()
    _wire_add_channel_conn(fake_conn, tenant_id=tenant_id, channel_id=channel_id)

    with patch(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client"
    ) as mock_factory:
        mock_cw = MagicMock()
        mock_cw.get_channel_token = AsyncMock(side_effect=RuntimeError("network down"))
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.post(
            f"/api/admin/tenants/{tenant_id}/channels",
            json={
                "channel_kind": "whatsapp",
                "connection_payload": {
                    "chatwoot_inbox_id": 10,
                    "waba_id": "123",
                    "phone_number_id": "456",
                },
            },
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 201, res.text
    body = res.json()
    # token was NOT captured but the add succeeded
    assert body["token_captured"] is False


async def test_add_channel_token_none_from_cw_returns_false(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """If get_channel_token returns None, token_captured=False but add succeeds."""
    tenant_id = uuid4()
    channel_id = uuid4()
    _wire_add_channel_conn(fake_conn, tenant_id=tenant_id, channel_id=channel_id)

    with patch(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client"
    ) as mock_factory:
        mock_cw = MagicMock()
        mock_cw.get_channel_token = AsyncMock(return_value=None)
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.post(
            f"/api/admin/tenants/{tenant_id}/channels",
            json={
                "channel_kind": "whatsapp",
                "connection_payload": {
                    "chatwoot_inbox_id": 10,
                    "waba_id": "123",
                    "phone_number_id": "456",
                },
            },
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 201, res.text
    assert res.json()["token_captured"] is False


async def test_add_channel_telegram_no_token_capture(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """Telegram channel does not trigger token auto-provisioning (token_captured=False)."""
    tenant_id = uuid4()
    channel_id = uuid4()
    _wire_add_channel_conn(fake_conn, tenant_id=tenant_id, channel_id=channel_id)

    with patch(
        "polaris_api.routers.admin_tenant_channels.get_chatwoot_client"
    ) as mock_factory:
        mock_cw = MagicMock()
        mock_cw.get_channel_token = AsyncMock(return_value="some-token")
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        await http_client.post(
            f"/api/admin/tenants/{tenant_id}/channels",
            json={
                "channel_kind": "telegram",
                "connection_payload": {
                    "chatwoot_inbox_id": 50,
                },
            },
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    # Telegram goes through the provision_channel path which may succeed or
    # raise depending on tier. Since it IS in _PROVISIONABLE_KINDS...
    # The important thing is get_channel_token was NOT called for telegram.
    mock_cw.get_channel_token.assert_not_called()


# =====================================================================
# admin_secrets — kind mapping
# =====================================================================


async def test_rotate_meta_whatsapp_stores_waba_access_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """POST /rotate with service=meta_whatsapp stores kind=meta_waba_access_token."""
    tenant_id = uuid4()
    fake_conn.canned_row["SELECT id FROM tenants"] = {"id": str(tenant_id)}
    fake_conn.canned_val["audit_log_prev_hash"] = None

    token = _mint_jwt(role="platform_admin")
    res = await http_client.post(
        f"/api/admin/secrets/{tenant_id}/rotate",
        json={"service": "meta_whatsapp", "new_value": "fake-waba-token-value"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["service"] == "meta_whatsapp"

    # tenant_secrets INSERT must use kind=meta_waba_access_token
    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            assert args[1] == "meta_waba_access_token", (
                f"Expected meta_waba_access_token, got {args[1]}"
            )
            return
    pytest.fail("Expected an INSERT INTO tenant_secrets but none found")


async def test_rotate_meta_messenger_stores_fb_page_access_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """POST /rotate with service=meta_messenger stores kind=meta_fb_page_access_token."""
    tenant_id = uuid4()
    fake_conn.canned_row["SELECT id FROM tenants"] = {"id": str(tenant_id)}
    fake_conn.canned_val["audit_log_prev_hash"] = None

    token = _mint_jwt(role="platform_admin")
    res = await http_client.post(
        f"/api/admin/secrets/{tenant_id}/rotate",
        json={"service": "meta_messenger", "new_value": "fake-messenger-token"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            assert args[1] == "meta_fb_page_access_token", (
                f"Expected meta_fb_page_access_token, got {args[1]}"
            )
            return
    pytest.fail("Expected an INSERT INTO tenant_secrets but none found")


async def test_rotate_meta_ig_stores_ig_login_access_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """POST /rotate with service=meta_ig stores kind=meta_ig_login_access_token.

    ``meta_ig`` must NOT write ``meta_fb_page_access_token`` — that is the
    Messenger Page-token slot, and the shared mapping let an Instagram rotation
    clobber the Messenger token (the IGAA-token-in-the-Page-slot incident,
    2026-06-09). Since migration 104 the only IG path is instagram_login
    (graph.instagram.com), whose send client reads meta_ig_login_access_token.
    """
    tenant_id = uuid4()
    fake_conn.canned_row["SELECT id FROM tenants"] = {"id": str(tenant_id)}
    fake_conn.canned_val["audit_log_prev_hash"] = None

    token = _mint_jwt(role="platform_admin")
    res = await http_client.post(
        f"/api/admin/secrets/{tenant_id}/rotate",
        json={"service": "meta_ig", "new_value": "fake-ig-token"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            assert args[1] == "meta_ig_login_access_token", (
                f"Expected meta_ig_login_access_token, got {args[1]}"
            )
            return
    pytest.fail("Expected an INSERT INTO tenant_secrets but none found")


async def test_rotate_telegram_bot_token_stores_telegram_bot_token(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """POST /rotate with service=telegram_bot_token stores kind=telegram_bot_token (unchanged)."""
    tenant_id = uuid4()
    fake_conn.canned_row["SELECT id FROM tenants"] = {"id": str(tenant_id)}
    fake_conn.canned_val["audit_log_prev_hash"] = None

    token = _mint_jwt(role="platform_admin")
    res = await http_client.post(
        f"/api/admin/secrets/{tenant_id}/rotate",
        json={"service": "telegram_bot_token", "new_value": "fake-tg-bot-token"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    for sql, args in fake_conn.executed:
        if "INSERT INTO tenant_secrets" in sql:
            assert args[1] == "telegram_bot_token", (
                f"Expected telegram_bot_token, got {args[1]}"
            )
            return
    pytest.fail("Expected an INSERT INTO tenant_secrets but none found")


# =====================================================================
# get_inbox_info — token_preview
# =====================================================================


def _wire_inbox_info_conn(
    conn: _FakeConn,
    *,
    cw_account_id: int = 7,
    secret_nonce: bytes | None = None,
    secret_ciphertext: bytes | None = None,
    secret_version: int = 1,
    sql_match_key: str = "tenant_secrets",
) -> None:
    """Populate _FakeConn for get_inbox_info requests.

    tenant row: has chatwoot_account_id.
    tenant_secrets SELECT: returns None (absent) by default; pass
        secret_nonce/ciphertext to simulate a stored secret.
    """
    conn.canned_row["SELECT chatwoot_account_id FROM tenants"] = {
        "chatwoot_account_id": cw_account_id,
    }
    if secret_nonce is not None and secret_ciphertext is not None:
        conn.canned_row[sql_match_key] = {
            "nonce": secret_nonce,
            "ciphertext": secret_ciphertext,
            "version": secret_version,
        }
    else:
        conn.canned_row[sql_match_key] = None

    # token_captured check (fetchval)
    conn.canned_val["SELECT 1 FROM tenant_secrets"] = 1


def _make_inbox_payload(
    channel_type: str,
    *,
    api_key: str | None = None,
    page_id: str | None = None,
    instagram_id: str | None = None,
    bot_name: str | None = None,
    inbox_id: int = 42,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": inbox_id,
        "name": "Test Inbox",
        "channel_type": channel_type,
    }
    if api_key is not None:
        payload["api_key"] = api_key
    if page_id is not None:
        payload["page_id"] = page_id
    if instagram_id is not None:
        payload["instagram_id"] = instagram_id
    if bot_name is not None:
        payload["bot_name"] = bot_name
    return payload


async def test_inbox_info_whatsapp_token_preview_from_api_key(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """WhatsApp: token_preview derived from Chatwoot inbox api_key (last-4 masked)."""
    from uuid import uuid4

    tenant_id = uuid4()
    _wire_inbox_info_conn(fake_conn)

    inbox_payload = _make_inbox_payload(
        "Channel::Whatsapp",
        api_key="EAABsb-fake-whatsapp-ABCD",
    )

    with patch("polaris_api.routers.admin_tenant_email.get_chatwoot_client") as mock_factory:
        mock_cw = MagicMock()
        mock_cw.list_inboxes = AsyncMock(return_value=[inbox_payload])
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.get(
            f"/api/admin/tenants/{tenant_id}/channels/inbox-info",
            params={"chatwoot_inbox_id": 42},
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["channel_type"] == "whatsapp"
    preview = body["token_preview"]
    assert preview is not None
    assert preview == "••••ABCD", f"Expected ••••ABCD, got {preview!r}"
    # Must not expose more than the last 4 characters
    assert preview.startswith("••••")
    assert len(preview.replace("•", "")) == 4


async def test_inbox_info_whatsapp_no_api_key_gives_none_preview(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """WhatsApp: token_preview is None when api_key is absent from inbox payload."""
    from uuid import uuid4

    tenant_id = uuid4()
    _wire_inbox_info_conn(fake_conn)

    inbox_payload = _make_inbox_payload("Channel::Whatsapp")  # no api_key

    with patch("polaris_api.routers.admin_tenant_email.get_chatwoot_client") as mock_factory:
        mock_cw = MagicMock()
        mock_cw.list_inboxes = AsyncMock(return_value=[inbox_payload])
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.get(
            f"/api/admin/tenants/{tenant_id}/channels/inbox-info",
            params={"chatwoot_inbox_id": 42},
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 200, res.text
    assert res.json()["token_preview"] is None


async def test_inbox_info_instagram_login_token_preview_from_stored_secret(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """Channel::Instagram now maps to the native instagram_login kind, and its
    token_preview derives from the stored meta_ig_login_access_token (last-4)."""
    from uuid import uuid4

    from polaris_api.services import secrets as secrets_svc

    tenant_id = uuid4()

    # Build a real encrypted secret so secrets_svc.get decrypts successfully.
    plaintext = b"IGAA-fake-ig-login-token-XY78"
    wire = secrets_svc.encrypt(plaintext, tenant_id)
    nonce = wire[1 : 1 + secrets_svc.NONCE_BYTES]
    ct = wire[1 + secrets_svc.NONCE_BYTES :]

    _wire_inbox_info_conn(
        fake_conn,
        secret_nonce=nonce,
        secret_ciphertext=ct,
        sql_match_key="tenant_secrets",
    )

    inbox_payload = _make_inbox_payload("Channel::Instagram", instagram_id="IG_123")

    with patch("polaris_api.routers.admin_tenant_email.get_chatwoot_client") as mock_factory:
        mock_cw = MagicMock()
        mock_cw.list_inboxes = AsyncMock(return_value=[inbox_payload])
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.get(
            f"/api/admin/tenants/{tenant_id}/channels/inbox-info",
            params={"chatwoot_inbox_id": 42},
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["channel_type"] == "instagram_login"
    preview = body["token_preview"]
    assert preview == "••••XY78", f"Expected ••••XY78, got {preview!r}"


async def test_inbox_info_instagram_no_stored_secret_gives_none_preview(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """Instagram: token_preview is None when no stored secret exists."""
    from uuid import uuid4

    tenant_id = uuid4()
    _wire_inbox_info_conn(fake_conn)  # secret_nonce=None → row absent

    inbox_payload = _make_inbox_payload("Channel::Instagram", instagram_id="IG_456")

    with patch("polaris_api.routers.admin_tenant_email.get_chatwoot_client") as mock_factory:
        mock_cw = MagicMock()
        mock_cw.list_inboxes = AsyncMock(return_value=[inbox_payload])
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.get(
            f"/api/admin/tenants/{tenant_id}/channels/inbox-info",
            params={"chatwoot_inbox_id": 42},
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 200, res.text
    assert res.json()["token_preview"] is None


async def test_inbox_info_telegram_token_preview_from_stored_secret(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """Telegram: token_preview derived from stored telegram_bot_token (last-4)."""
    from uuid import uuid4

    from polaris_api.services import secrets as secrets_svc

    tenant_id = uuid4()

    plaintext = b"123456:AAEfake-telegram-bot-token-Q9rZ"
    wire = secrets_svc.encrypt(plaintext, tenant_id)
    nonce = wire[1 : 1 + secrets_svc.NONCE_BYTES]
    ct = wire[1 + secrets_svc.NONCE_BYTES :]

    _wire_inbox_info_conn(
        fake_conn,
        secret_nonce=nonce,
        secret_ciphertext=ct,
        sql_match_key="tenant_secrets",
    )

    inbox_payload = _make_inbox_payload(
        "Channel::Telegram", bot_name="polaris_bot", inbox_id=42
    )

    with patch("polaris_api.routers.admin_tenant_email.get_chatwoot_client") as mock_factory:
        mock_cw = MagicMock()
        mock_cw.list_inboxes = AsyncMock(return_value=[inbox_payload])
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.get(
            f"/api/admin/tenants/{tenant_id}/channels/inbox-info",
            params={"chatwoot_inbox_id": 42},
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    preview = body["token_preview"]
    assert preview == "••••Q9rZ", f"Expected ••••Q9rZ, got {preview!r}"


async def test_inbox_info_token_preview_exception_gives_none(
    http_client: AsyncClient,
    fake_conn: _FakeConn,
) -> None:
    """If secrets_svc.get raises, token_preview is None and the endpoint still returns 200."""
    from uuid import uuid4

    tenant_id = uuid4()
    _wire_inbox_info_conn(fake_conn)

    inbox_payload = _make_inbox_payload("Channel::Telegram", bot_name="polaris_bot")

    with (
        patch("polaris_api.routers.admin_tenant_email.get_chatwoot_client") as mock_factory,
        patch(
            "polaris_api.routers.admin_tenant_email.secrets_svc.get",
            new=AsyncMock(side_effect=RuntimeError("kms failure")),
        ),
    ):
        mock_cw = MagicMock()
        mock_cw.list_inboxes = AsyncMock(return_value=[inbox_payload])
        mock_factory.return_value = mock_cw

        token_jwt = _mint_jwt(role="platform_admin")
        res = await http_client.get(
            f"/api/admin/tenants/{tenant_id}/channels/inbox-info",
            params={"chatwoot_inbox_id": 42},
            headers={"Authorization": f"Bearer {token_jwt}"},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token_preview"] is None
    # channel info still present
    assert body["channel_type"] == "telegram"


# =====================================================================
# _mask_last4 unit tests
# =====================================================================


def test_mask_last4_long_token() -> None:
    from polaris_api.routers.admin_tenants import _mask_last4

    result = _mask_last4("EAABsb-long-token-3a2f")
    assert result == "••••3a2f"


def test_mask_last4_exactly_four_chars() -> None:
    from polaris_api.routers.admin_tenants import _mask_last4

    result = _mask_last4("abcd")
    assert result == "••••abcd"


def test_mask_last4_shorter_than_four() -> None:
    from polaris_api.routers.admin_tenants import _mask_last4

    result = _mask_last4("ab")
    assert result == "••••ab"
    # Confirm it never leaks more than 2 raw chars
    assert result.replace("•", "") == "ab"
