"""Phase 1 PR-1 — cross-tenant RLS negative tests.

Proves that the RLS policies on `tenants`, `tenant_users`, and `tenant_channels`
make cross-tenant rows invisible to a session that has set
`app.current_tenant_id` to a different tenant's UUID.

Strategy
--------
1. The session-scoped `service_dsn` fixture (conftest.py) created a
   `polaris_test_anon` role with `NOBYPASSRLS` and granted DML on the Phase 1
   tables. Connect as the service role (BYPASSRLS) to seed test data.
2. For each test, open a transaction, `SET LOCAL ROLE polaris_test_anon`, and
   `SELECT set_config('app.current_tenant_id', $1, true)` (the function form
   that accepts asyncpg parameters; the SET LOCAL statement-form does not bind
   placeholders). The transaction is rolled back at the end to discard the
   local role/setting switch.
3. Cross-tenant SELECT must return zero rows. Same-tenant SELECT must return
   one row (positive control — guards against false negatives from empty
   tables or broken fixtures).

PR-1 design boundary: this is a black-box DB test. No FastAPI, no TenantContext,
no dual asyncpg pool. PR-2 layers those on top; PR-1 proves the DB invariant.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg
import pytest


async def _seed_tenant(conn: asyncpg.Connection, slug: str) -> UUID:
    """Insert a minimal `tenants` row via the BYPASSRLS service role. Returns id."""
    row = await conn.fetchrow(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id",
        slug,
    )
    assert row is not None
    return row["id"]


async def _seed_auth_user(conn: asyncpg.Connection, email: str) -> UUID:
    row = await conn.fetchrow(
        "INSERT INTO auth.users (email) VALUES ($1) RETURNING id",
        email,
    )
    assert row is not None
    return row["id"]


async def _seed_channel(conn: asyncpg.Connection, tenant_id: UUID, inbox_id: int) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_channels (tenant_id, channel, chatwoot_inbox_id)
        VALUES ($1, 'whatsapp', $2)
        RETURNING id
        """,
        tenant_id,
        inbox_id,
    )
    assert row is not None
    return row["id"]


# ----------------------------------------------------------------------------
# Sanity: tables exist with RLS + FORCE RLS enabled
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["tenants", "tenant_users", "tenant_channels"])
async def test_force_rls_enabled(service_conn: asyncpg.Connection, table: str) -> None:
    """Every Phase 1 PR-1 table must have RLS enabled AND FORCE RLS set.

    `ENABLE` alone exempts the table owner; `FORCE` is what makes the policy
    apply universally. See [04b_PHASE_1_DEEP.md §9 gotcha #3].
    """
    row = await service_conn.fetchrow(
        """
        SELECT relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE oid = ($1 || '.' || $2)::regclass
        """,
        "public",
        table,
    )
    assert row is not None, f"table public.{table} missing"
    assert row["relrowsecurity"] is True, f"{table}: RLS not enabled"
    assert row["relforcerowsecurity"] is True, f"{table}: FORCE RLS not set"


# ----------------------------------------------------------------------------
# tenants — cross-tenant invisibility + positive control
# ----------------------------------------------------------------------------


async def test_tenants_cross_tenant_select_returns_zero_rows(
    service_conn: asyncpg.Connection,
) -> None:
    tenant_a = await _seed_tenant(service_conn, slug="pytest-tenants-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-tenants-b")

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute(f"SET LOCAL ROLE {'polaris_test_anon'}")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )

        # Cross-tenant: must return 0 rows.
        cross = await service_conn.fetch(
            "SELECT id FROM tenants WHERE id = $1", tenant_b
        )
        assert cross == [], "RLS leak: tenant_a saw tenant_b's tenants row"

        # Positive control: same-tenant must return 1 row.
        own = await service_conn.fetch("SELECT id FROM tenants WHERE id = $1", tenant_a)
        assert len(own) == 1
        assert own[0]["id"] == tenant_a
    finally:
        await tx.rollback()


async def test_tenants_blocks_anon_insert_without_policy(
    service_conn: asyncpg.Connection,
) -> None:
    """`tenants` has no INSERT policy for anon — defense-in-depth against tenant
    creation from a tenant-scoped path. Should raise an RLS error.
    """
    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(uuid4())
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await service_conn.execute(
                "INSERT INTO tenants (slug) VALUES ($1)", "pytest-anon-attempt"
            )
    finally:
        await tx.rollback()


# ----------------------------------------------------------------------------
# tenant_users — cross-tenant invisibility
# ----------------------------------------------------------------------------


async def test_tenant_users_cross_tenant_select_returns_zero_rows(
    service_conn: asyncpg.Connection,
) -> None:
    tenant_a = await _seed_tenant(service_conn, slug="pytest-users-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-users-b")
    user_a = await _seed_auth_user(service_conn, email="pytest-users-a@pytest.invalid")
    user_b = await _seed_auth_user(service_conn, email="pytest-users-b@pytest.invalid")
    await service_conn.execute(
        "INSERT INTO tenant_users (tenant_id, user_id, role) VALUES ($1, $2, 'admin')",
        tenant_a,
        user_a,
    )
    await service_conn.execute(
        "INSERT INTO tenant_users (tenant_id, user_id, role) VALUES ($1, $2, 'admin')",
        tenant_b,
        user_b,
    )

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )

        cross = await service_conn.fetch(
            "SELECT user_id FROM tenant_users WHERE tenant_id = $1", tenant_b
        )
        assert cross == [], "RLS leak: tenant_a saw tenant_b's tenant_users row"

        own = await service_conn.fetch(
            "SELECT user_id FROM tenant_users WHERE tenant_id = $1", tenant_a
        )
        assert len(own) == 1
        assert own[0]["user_id"] == user_a
    finally:
        await tx.rollback()


# ----------------------------------------------------------------------------
# tenant_channels — cross-tenant invisibility + cross-tenant INSERT blocked
# ----------------------------------------------------------------------------


async def test_tenant_channels_cross_tenant_select_returns_zero_rows(
    service_conn: asyncpg.Connection,
) -> None:
    tenant_a = await _seed_tenant(service_conn, slug="pytest-channels-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-channels-b")
    # Distinct chatwoot_inbox_id values (UNIQUE constraint).
    await _seed_channel(service_conn, tenant_a, inbox_id=9_000_001)
    await _seed_channel(service_conn, tenant_b, inbox_id=9_000_002)

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )

        cross = await service_conn.fetch(
            "SELECT id FROM tenant_channels WHERE tenant_id = $1", tenant_b
        )
        assert cross == [], "RLS leak: tenant_a saw tenant_b's tenant_channels row"

        own = await service_conn.fetch(
            "SELECT id FROM tenant_channels WHERE tenant_id = $1", tenant_a
        )
        assert len(own) == 1
    finally:
        await tx.rollback()


async def test_tenant_channels_cross_tenant_insert_blocked(
    service_conn: asyncpg.Connection,
) -> None:
    """The FOR ALL policy on tenant_channels carries a WITH CHECK clause that
    blocks INSERT rows whose tenant_id != current_setting('app.current_tenant_id').
    """
    tenant_a = await _seed_tenant(service_conn, slug="pytest-chans-insert-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-chans-insert-b")

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        # Attempt to insert under tenant_b while session belongs to tenant_a.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await service_conn.execute(
                """
                INSERT INTO tenant_channels (tenant_id, channel, chatwoot_inbox_id)
                VALUES ($1, 'whatsapp', $2)
                """,
                tenant_b,
                9_000_999,
            )
    finally:
        await tx.rollback()


# ----------------------------------------------------------------------------
# Implicit deny — no setting → zero rows
# ----------------------------------------------------------------------------


async def test_rls_blocks_when_setting_absent(service_conn: asyncpg.Connection) -> None:
    """When `app.current_tenant_id` is unset, the policy expression
    `current_setting(..., true)::uuid` is NULL, so the equality check is NULL
    → no rows match → implicit deny. Verifies the missing_ok=true guard works
    as a safety net for unauthenticated/misconfigured paths.
    """
    await _seed_tenant(service_conn, slug="pytest-implicit-deny")

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        # Deliberately do NOT set app.current_tenant_id.
        rows = await service_conn.fetch(
            "SELECT id FROM tenants WHERE slug = $1", "pytest-implicit-deny"
        )
        assert rows == [], "RLS leak: anon role saw rows with no tenant setting"
    finally:
        await tx.rollback()
