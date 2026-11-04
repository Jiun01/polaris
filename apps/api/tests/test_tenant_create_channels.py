"""Change A — tenant create accepts an allowed_channel_kinds override.

Asserts: override persisted to the tenant_quotas INSERT + audit; omitted →
tier default; invalid/empty kinds → 422 (Pydantic validation, pre-DB).

Router-level test with a stubbed asyncpg connection — no real DB.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
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
os.environ.setdefault(
    "POLARIS_KMS_KEY",
    "00112233445566778899aabbccddeeff" "ffeeddccbbaa99887766554433221100",
)

from polaris_api.main import app  # noqa: E402
from polaris_api.services.admin import get_admin_service_conn  # noqa: E402

_PRO_DEFAULT = ["whatsapp", "instagram_login", "messenger", "telegram", "email"]


def _mint_jwt(*, role: str = "platform_admin") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "role": role,
            "iss": _TEST_JWT_ISSUER,
            "iat": now,
            "exp": now + 300,
        },
        _TEST_JWT_SECRET,
        algorithm="HS256",
    )


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetched: list[tuple[str, tuple[Any, ...]]] = []
        self.canned_val: dict[str, Any] = {}

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetched.append((sql, args))
        for key, val in self.canned_val.items():
            if key in sql:
                return val
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"


@pytest_asyncio.fixture
async def fake_conn() -> AsyncIterator[_FakeConn]:
    conn = _FakeConn()
    tenant_id = uuid4()
    conn.canned_val["INSERT INTO tenants"] = tenant_id
    conn.canned_val["upsert_auth_user_by_email"] = uuid4()

    async def override() -> AsyncIterator[_FakeConn]:
        yield conn

    app.dependency_overrides[get_admin_service_conn] = override
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _base_payload(**overrides: Any) -> dict[str, Any]:
    body = {
        "display_name": "Acme Co",
        "slug": "acme-co",
        "plan_tier": "pro",
        "billing_cycle": "monthly",
        "admin_email": "admin@acme.my",
        "admin_display_name": "Jane Tan",
    }
    body.update(overrides)
    return body


def _quotas_insert_kinds(conn: _FakeConn) -> Any:
    """Return the allowed_channel_kinds arg ($5 → index 4) of the quotas INSERT."""
    for sql, args in conn.executed:
        if "INSERT INTO tenant_quotas" in sql:
            return args[4]
    raise AssertionError(f"no tenant_quotas INSERT; saw {[s[:40] for s, _ in conn.executed]}")


def _tenants_insert_settings(conn: _FakeConn) -> Any:
    """Return the settings dict arg ($4 → index 3) of the tenants INSERT.

    NOTE: the tenants row is created via ``fetchval`` (RETURNING id), not
    ``execute`` — so it lands in ``conn.fetched`` rather than ``conn.executed``.
    """
    for sql, args in conn.fetched:
        if "INSERT INTO tenants" in sql:
            return args[3]
    raise AssertionError(
        f"no tenants INSERT; saw {[s[:40] for s, _ in conn.fetched]}"
    )


async def test_override_persisted(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    with (
        patch("polaris_api.routers.admin_tenants.audit_record", AsyncMock()),
        patch(
            "polaris_api.routers.admin_tenants.issue_portal_invite",
            AsyncMock(
                return_value=SimpleNamespace(
                    dispatched=True, token_inserted=True, failure_reason=None
                )
            ),
        ),
    ):
        res = await http_client.post(
            "/api/admin/tenants",
            json=_base_payload(allowed_channel_kinds=["whatsapp", "messenger"]),
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 201, res.text
    assert _quotas_insert_kinds(fake_conn) == ["whatsapp", "messenger"]


async def test_omitted_uses_tier_default(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    with (
        patch("polaris_api.routers.admin_tenants.audit_record", AsyncMock()),
        patch(
            "polaris_api.routers.admin_tenants.issue_portal_invite",
            AsyncMock(
                return_value=SimpleNamespace(
                    dispatched=True, token_inserted=True, failure_reason=None
                )
            ),
        ),
    ):
        res = await http_client.post(
            "/api/admin/tenants",
            json=_base_payload(),  # no allowed_channel_kinds
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 201, res.text
    assert _quotas_insert_kinds(fake_conn) == _PRO_DEFAULT


async def test_invalid_kind_422(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    res = await http_client.post(
        "/api/admin/tenants",
        json=_base_payload(allowed_channel_kinds=["instagram"]),  # legacy → invalid
        headers={"Authorization": f"Bearer {_mint_jwt()}"},
    )
    assert res.status_code == 422, res.text


async def test_empty_list_422(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    res = await http_client.post(
        "/api/admin/tenants",
        json=_base_payload(allowed_channel_kinds=[]),
        headers={"Authorization": f"Bearer {_mint_jwt()}"},
    )
    assert res.status_code == 422, res.text


async def test_new_tenant_seeds_agent_enabled_by_default(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Agent-screen redesign: new tenants get the tool-calling agent ON with a
    sensible default tool set, so the bot works out of the box."""
    with (
        patch("polaris_api.routers.admin_tenants.audit_record", AsyncMock()),
        patch(
            "polaris_api.routers.admin_tenants.issue_portal_invite",
            AsyncMock(
                return_value=SimpleNamespace(
                    dispatched=True, token_inserted=True, failure_reason=None
                )
            ),
        ),
    ):
        res = await http_client.post(
            "/api/admin/tenants",
            json=_base_payload(),
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 201, res.text
    settings = _tenants_insert_settings(fake_conn)
    assert isinstance(settings, dict)
    agent = settings["agent_config"]
    assert agent["enabled"] is True
    assert "search_knowledge" in agent["enabled_tools"]
    assert "handoff_to_human" in agent["enabled_tools"]
