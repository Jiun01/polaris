"""POST /api/admin/ingestion/force-run — priority flip + audit + scope gate.

PRD §6.2.1: platform_admin endpoint that flips matching ``pending``
rows to ``priority='high'`` so the desktop drains them first on the
next cron tick / GUI invocation. The endpoint does NOT trigger the
desktop directly (workstation may be offline); the 202 + run_token
acknowledge receipt only.

PR-4 hardening exercised here:
  * H1 — Empty body requires ``confirm_global=true`` (422 otherwise).
  * H2 — Each distinct affected tenant gets one ``audit_logs`` row
    via ``services.audit.record``; the test patches the import to
    assert call shape without writing to a real DB.
  * M2 — All UPDATE branches carry ``AND priority = 'normal'`` so
    re-runs on already-high rows don't burn txids.

Auth gates tested:
  * platform_admin → 202 + flipped count.
  * platform_service (machines) → 403 (force-run is human-only).
  * tenant_admin → 403.
  * Missing JWT → 401.

Scope variants:
  * tenant_id only → tenant-scoped UPDATE.
  * request_id only → row-scoped UPDATE.
  * both → row-scoped UPDATE with belt-and-braces tenant_id check.
  * neither WITHOUT confirm_global → 422 (PR-4 H1 fix).
  * neither WITH confirm_global=true → global UPDATE.
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

_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)

from polaris_api.main import app  # noqa: E402
from polaris_api.services.admin import get_admin_service_conn  # noqa: E402

# See test_admin_chunking_config.py + conftest.py for the loop_scope
# rationale (PR-5 review M6). TL;DR: dependency-override admin tests
# poison the session event loop without this pin; root cause TBD.
pytestmark = pytest.mark.asyncio(loop_scope="function")


def _mint(
    *,
    role: str,
    tenant_id: UUID | None = None,
    sub: UUID | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(sub or uuid4()),
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


class _TxCtx:
    """Async context manager double for ``conn.transaction()``.

    The handler opens an inner transaction around the audit writes;
    AsyncMock's auto-magic context handling sometimes mis-fires for
    nested async contexts, so we hand-roll the minimum shape.
    """

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_conn() -> AsyncIterator[tuple[AsyncClient, AsyncMock]]:
    conn = AsyncMock()
    # Default fetch result: zero rows affected. Per-test overrides set
    # specific row shapes to exercise the audit-call counting.
    conn.fetch = AsyncMock(return_value=[])
    conn.transaction = MagicMock(return_value=_TxCtx())

    async def _override() -> AsyncIterator[AsyncMock]:
        yield conn

    app.dependency_overrides[get_admin_service_conn] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, conn
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


def _affected_row(tenant_id: UUID) -> dict[str, Any]:
    """One row shape returned by UPDATE...RETURNING tenant_id."""
    return {"tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_platform_admin_flips_global_pending_with_confirm(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """PR-4 H1: global flip requires explicit confirm_global=true."""
    client, conn = client_and_conn
    tenant_a, tenant_b = uuid4(), uuid4()
    conn.fetch = AsyncMock(
        return_value=[
            _affected_row(tenant_a),
            _affected_row(tenant_a),
            _affected_row(tenant_b),
        ]
    )
    with patch(
        "polaris_api.routers.admin_ingestion.audit_record",
        new=AsyncMock(return_value=uuid4()),
    ) as audit_mock:
        resp = await client.post(
            "/api/admin/ingestion/force-run",
            headers={"Authorization": f"Bearer {_mint(role='platform_admin')}"},
            json={"confirm_global": True},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] == 3
    UUID(body["run_token"])
    # Two distinct tenants → two audit rows.
    assert audit_mock.await_count == 2


async def test_empty_body_without_confirm_global_returns_422(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """PR-4 H1: typo'd empty body must NOT silently flip everything."""
    client, _ = client_and_conn
    resp = await client.post(
        "/api/admin/ingestion/force-run",
        headers={"Authorization": f"Bearer {_mint(role='platform_admin')}"},
        json={},
    )
    assert resp.status_code == 422, resp.text
    # Pydantic surfaces the model_validator message in the error detail.
    assert "confirm_global" in resp.text.lower()


async def test_platform_service_jwt_returns_403(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """force-run is human-only; the desktop's service-role token shouldn't
    be able to re-prioritise its own backlog."""
    client, _ = client_and_conn
    resp = await client.post(
        "/api/admin/ingestion/force-run",
        headers={"Authorization": f"Bearer {_mint(role='platform_service')}"},
        json={"confirm_global": True},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "platform_admin_required"


async def test_tenant_admin_jwt_returns_403(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, _ = client_and_conn
    tenant_id = uuid4()
    resp = await client.post(
        "/api/admin/ingestion/force-run",
        headers={
            "Authorization": f"Bearer {_mint(role='admin', tenant_id=tenant_id)}"
        },
        json={"confirm_global": True},
    )
    assert resp.status_code == 403, resp.text


async def test_missing_jwt_returns_401(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, _ = client_and_conn
    resp = await client.post("/api/admin/ingestion/force-run", json={})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scope variants
# ---------------------------------------------------------------------------


async def test_tenant_scoped_flip_uses_tenant_predicate(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """PR-4 M2: WHERE clause carries `priority='normal'`."""
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetch = AsyncMock(return_value=[_affected_row(tenant_id) for _ in range(2)])
    with patch(
        "polaris_api.routers.admin_ingestion.audit_record",
        new=AsyncMock(return_value=uuid4()),
    ) as audit_mock:
        resp = await client.post(
            "/api/admin/ingestion/force-run",
            headers={"Authorization": f"Bearer {_mint(role='platform_admin')}"},
            json={"tenant_id": str(tenant_id)},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 2
    sql, *bind_args = conn.fetch.await_args.args
    assert "WHERE tenant_id = $1" in sql
    assert "AND priority = 'normal'" in sql
    assert "RETURNING tenant_id" in sql
    assert bind_args == [tenant_id]
    # Single tenant → exactly one audit row regardless of row count.
    assert audit_mock.await_count == 1
    audit_kwargs = audit_mock.await_args.kwargs
    assert audit_kwargs["tenant_id"] == tenant_id
    assert audit_kwargs["action"] == "ingestion.force_run"
    assert audit_kwargs["after"]["rows_flipped"] == 2
    assert audit_kwargs["after"]["scope"] == "tenant"


async def test_request_scoped_flip_uses_request_predicate(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    client, conn = client_and_conn
    request_id = uuid4()
    tenant_id = uuid4()
    conn.fetch = AsyncMock(return_value=[_affected_row(tenant_id)])
    with patch(
        "polaris_api.routers.admin_ingestion.audit_record",
        new=AsyncMock(return_value=uuid4()),
    ):
        resp = await client.post(
            "/api/admin/ingestion/force-run",
            headers={"Authorization": f"Bearer {_mint(role='platform_admin')}"},
            json={"request_id": str(request_id)},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 1
    sql, *bind_args = conn.fetch.await_args.args
    assert "WHERE id = $1" in sql
    assert "AND priority = 'normal'" in sql
    assert bind_args == [request_id]


async def test_both_ids_includes_tenant_belt_and_braces(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """When both supplied, the row-scoped path also asserts tenant_id."""
    client, conn = client_and_conn
    tenant_id = uuid4()
    request_id = uuid4()
    conn.fetch = AsyncMock(return_value=[_affected_row(tenant_id)])
    with patch(
        "polaris_api.routers.admin_ingestion.audit_record",
        new=AsyncMock(return_value=uuid4()),
    ):
        resp = await client.post(
            "/api/admin/ingestion/force-run",
            headers={"Authorization": f"Bearer {_mint(role='platform_admin')}"},
            json={"tenant_id": str(tenant_id), "request_id": str(request_id)},
        )
    assert resp.status_code == 202, resp.text
    sql, *bind_args = conn.fetch.await_args.args
    assert "WHERE id = $1" in sql
    assert "AND tenant_id = $2" in sql
    assert "AND priority = 'normal'" in sql
    assert bind_args == [request_id, tenant_id]


async def test_zero_affected_still_returns_202_no_audit(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """Empty RETURNING set = nothing to flip = no audit rows written."""
    client, conn = client_and_conn
    tenant_id = uuid4()
    conn.fetch = AsyncMock(return_value=[])
    with patch(
        "polaris_api.routers.admin_ingestion.audit_record",
        new=AsyncMock(return_value=uuid4()),
    ) as audit_mock:
        resp = await client.post(
            "/api/admin/ingestion/force-run",
            headers={"Authorization": f"Bearer {_mint(role='platform_admin')}"},
            json={"tenant_id": str(tenant_id)},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 0
    assert audit_mock.await_count == 0


# ---------------------------------------------------------------------------
# Audit shape
# ---------------------------------------------------------------------------


async def test_audit_actor_pulled_from_jwt_sub(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """PR-4 H2: actor field on audit_logs row = JWT sub claim."""
    client, conn = client_and_conn
    tenant_id = uuid4()
    admin_user_id = uuid4()
    conn.fetch = AsyncMock(return_value=[_affected_row(tenant_id)])
    with patch(
        "polaris_api.routers.admin_ingestion.audit_record",
        new=AsyncMock(return_value=uuid4()),
    ) as audit_mock:
        resp = await client.post(
            "/api/admin/ingestion/force-run",
            headers={
                "Authorization": f"Bearer {_mint(role='platform_admin', sub=admin_user_id)}"
            },
            json={"tenant_id": str(tenant_id)},
        )
    assert resp.status_code == 202, resp.text
    audit_kwargs = audit_mock.await_args.kwargs
    assert audit_kwargs["actor"] == admin_user_id
    assert audit_kwargs["before"] == {"priority": "normal"}
    assert audit_kwargs["after"]["priority"] == "high"
    # run_token round-trips into the audit payload + the response body.
    assert audit_kwargs["after"]["run_token"] == resp.json()["run_token"]


async def test_audit_one_row_per_distinct_tenant(
    client_and_conn: tuple[AsyncClient, AsyncMock],
) -> None:
    """Multiple flipped rows for the same tenant collapse into one audit
    entry; the rows_flipped count carries the per-tenant total."""
    client, conn = client_and_conn
    tenant_a, tenant_b, tenant_c = uuid4(), uuid4(), uuid4()
    conn.fetch = AsyncMock(
        return_value=[
            _affected_row(tenant_a),
            _affected_row(tenant_a),
            _affected_row(tenant_a),
            _affected_row(tenant_b),
            _affected_row(tenant_c),
            _affected_row(tenant_c),
        ]
    )
    with patch(
        "polaris_api.routers.admin_ingestion.audit_record",
        new=AsyncMock(return_value=uuid4()),
    ) as audit_mock:
        resp = await client.post(
            "/api/admin/ingestion/force-run",
            headers={"Authorization": f"Bearer {_mint(role='platform_admin')}"},
            json={"confirm_global": True},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 6
    assert audit_mock.await_count == 3
    audited_tenants = {
        call.kwargs["tenant_id"] for call in audit_mock.await_args_list
    }
    assert audited_tenants == {tenant_a, tenant_b, tenant_c}
    audited_counts = {
        call.kwargs["tenant_id"]: call.kwargs["after"]["rows_flipped"]
        for call in audit_mock.await_args_list
    }
    assert audited_counts == {tenant_a: 3, tenant_b: 1, tenant_c: 2}
