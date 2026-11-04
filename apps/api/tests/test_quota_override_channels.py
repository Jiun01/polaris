"""Change B — quota override accepts the allowed_channel_kinds array.

Asserts: valid array bound to the UPDATE; invalid kind → 422; non-list value
for the array column → 422; an int column still works (regression guard).

Router-level test with a stubbed asyncpg connection — no real DB.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
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

_TENANT_ID = uuid4()


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


class _FakeRecord:
    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def __getitem__(self, k: str) -> Any:
        return self._d.get(k)


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "SELECT id FROM tenants WHERE id" in sql:
            return _FakeRecord({"id": str(_TENANT_ID)})
        if "FROM tenant_quotas WHERE tenant_id" in sql:
            return _FakeRecord(
                {"allowed_channel_kinds": ["whatsapp"], "bot_replies": 100}
            )
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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _url() -> str:
    return f"/api/admin/quotas/{_TENANT_ID}/override"


def _update_args(conn: _FakeConn) -> Any:
    for sql, args in conn.executed:
        if "UPDATE tenant_quotas SET allowed_channel_kinds" in sql:
            return args[0]
    raise AssertionError(
        f"no allowed_channel_kinds UPDATE; saw {[s[:50] for s, _ in conn.executed]}"
    )


async def test_array_bound_and_deduped(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    with patch("polaris_api.routers.admin_quotas.audit_record", AsyncMock()):
        res = await http_client.put(
            _url(),
            json={
                "column": "allowed_channel_kinds",
                "value": ["whatsapp", "messenger", "whatsapp"],
            },
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 200, res.text
    assert _update_args(fake_conn) == ["whatsapp", "messenger"]
    assert res.json()["new_value"] == ["whatsapp", "messenger"]


async def test_invalid_kind_422(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    with patch("polaris_api.routers.admin_quotas.audit_record", AsyncMock()):
        res = await http_client.put(
            _url(),
            json={"column": "allowed_channel_kinds", "value": ["bogus"]},
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 422, res.text


async def test_non_list_for_array_column_422(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    with patch("polaris_api.routers.admin_quotas.audit_record", AsyncMock()):
        res = await http_client.put(
            _url(),
            json={"column": "allowed_channel_kinds", "value": 5},
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 422, res.text


async def test_int_column_regression(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    with patch("polaris_api.routers.admin_quotas.audit_record", AsyncMock()):
        res = await http_client.put(
            _url(),
            json={"column": "bot_replies", "value": 25000},
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 200, res.text
    # The int column still binds via the UPDATE.
    assert any(
        "UPDATE tenant_quotas SET bot_replies" in s for s, _ in fake_conn.executed
    )


async def test_list_for_int_column_422(
    http_client: AsyncClient, fake_conn: _FakeConn
) -> None:
    with patch("polaris_api.routers.admin_quotas.audit_record", AsyncMock()):
        res = await http_client.put(
            _url(),
            json={"column": "bot_replies", "value": ["nope"]},
            headers={"Authorization": f"Bearer {_mint_jwt()}"},
        )
    assert res.status_code == 422, res.text
