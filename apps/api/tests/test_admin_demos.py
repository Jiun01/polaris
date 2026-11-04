"""Integration tests for the admin Demo System router — ADR-066.

Drives the handlers directly with a real BYPASSRLS connection (``service_conn``)
so the multi-table create transaction (tenants + quotas + non-login user +
audit) is validated against the real schema + SECURITY DEFINER user helper.
Requires POLARIS_DATABASE_URL / SUPABASE_DB_URL (skips otherwise).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from polaris_api.routers import admin_demos as ad

pytestmark = pytest.mark.asyncio(loop_scope="function")

_ADMIN = uuid4()


@pytest_asyncio.fixture(loop_scope="function")
async def service_conn(service_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    """Function-loop BYPASSRLS connection (the shared session-scoped service_conn
    fixture runs on a different loop than these function-scoped tests). Best-effort
    cleanup of pytest- demo rows on teardown."""
    conn = await asyncpg.connect(service_dsn)
    # Register the JSONB codec the app pools install (db/pools._init_conn) so
    # dict → $N::jsonb params work exactly as they do in production.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    try:
        yield conn
    finally:
        for sql in (
            "DELETE FROM tenant_users WHERE tenant_id IN "
            "(SELECT id FROM tenants WHERE slug LIKE 'pytest-%')",
            "DELETE FROM tenant_quotas WHERE tenant_id IN "
            "(SELECT id FROM tenants WHERE slug LIKE 'pytest-%')",
            "DELETE FROM demo_sessions WHERE tenant_id IN "
            "(SELECT id FROM tenants WHERE slug LIKE 'pytest-%')",
            "DELETE FROM audit_logs WHERE tenant_id IN "
            "(SELECT id FROM tenants WHERE slug LIKE 'pytest-%')",
            "DELETE FROM tenants WHERE slug LIKE 'pytest-%'",
            "DELETE FROM auth.users WHERE email LIKE 'demo+pytest-%'",
        ):
            try:
                await conn.execute(sql)
            except Exception:  # noqa: BLE001, S110 — best-effort throwaway-DB cleanup
                pass
        await conn.close()


async def test_demo_create_crud_lifecycle(service_conn: asyncpg.Connection) -> None:
    # --- create -------------------------------------------------------------
    resp = await ad.create_demo(
        ad.DemoCreate(
            display_name="Pytest TRG Watch",
            demo_code="pytest-trgw-1824",
            duration_hours=6,
            max_messages=50,
            slug="pytest-trg-demo",
            description="Luxury watch retailer demo",
        ),
        service_conn,
        _ADMIN,
    )
    tid = UUID(resp.id)
    assert resp.slug == "pytest-trg-demo"

    # tenant row is a hidden demo
    row = await service_conn.fetchrow(
        "SELECT is_demo, demo_code, demo_duration_hours, demo_max_messages, "
        "settings->>'company_name' AS company FROM tenants WHERE id = $1",
        tid,
    )
    assert row["is_demo"] is True
    assert row["demo_code"] == "pytest-trgw-1824"
    assert row["demo_max_messages"] == 50
    assert row["company"] == "Pytest TRG Watch"

    # quota row + non-login user (for impersonation) were created
    assert (
        await service_conn.fetchval(
            "SELECT COUNT(*) FROM tenant_quotas WHERE tenant_id = $1", tid
        )
        == 1
    )
    assert (
        await service_conn.fetchval(
            "SELECT COUNT(*) FROM tenant_users WHERE tenant_id = $1", tid
        )
        == 1
    )

    # --- list excludes the gateway, includes the new demo -------------------
    demos = await ad.list_demos(service_conn, _ADMIN)
    ids = {d.id for d in demos}
    assert resp.id in ids
    assert str(ad._GATEWAY) not in ids

    # --- detail -------------------------------------------------------------
    detail = await ad.get_demo(tid, service_conn, _ADMIN)
    assert detail.demo_code == "pytest-trgw-1824"
    assert detail.active_sessions == 0

    # --- duplicate demo_code → 409 -----------------------------------------
    with pytest.raises(HTTPException) as exc:
        await ad.create_demo(
            ad.DemoCreate(
                display_name="Other",
                demo_code="pytest-trgw-1824",
                slug="pytest-other-demo",
            ),
            service_conn,
            _ADMIN,
        )
    assert exc.value.status_code == 409

    # --- patch caps + code --------------------------------------------------
    patched = await ad.update_demo(
        tid,
        ad.DemoPatch(max_messages=25, demo_code="pytest-trgw-9999"),
        service_conn,
        _ADMIN,
    )
    assert patched.max_messages == 25
    assert patched.demo_code == "pytest-trgw-9999"

    # --- sessions log empty -------------------------------------------------
    assert await ad.list_sessions(tid, service_conn, _ADMIN) == []

    # --- delete soft-deletes + frees the code ------------------------------
    await ad.delete_demo(tid, service_conn, _ADMIN)
    with pytest.raises(HTTPException) as exc2:
        await ad.get_demo(tid, service_conn, _ADMIN)
    assert exc2.value.status_code == 404

    # the freed code can be reused by a new demo
    resp2 = await ad.create_demo(
        ad.DemoCreate(
            display_name="Pytest TRG Reborn",
            demo_code="pytest-trgw-9999",
            slug="pytest-trg-demo-2",
        ),
        service_conn,
        _ADMIN,
    )
    assert resp2.id != resp.id


async def test_create_demo_rejects_bad_slug(service_conn: asyncpg.Connection) -> None:
    with pytest.raises(HTTPException) as exc:
        await ad.create_demo(
            ad.DemoCreate(
                display_name="X", demo_code="pytest-bad-0001", slug="Bad Slug!"
            ),
            service_conn,
            _ADMIN,
        )
    assert exc.value.status_code == 400
