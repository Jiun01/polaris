"""Phase 1 PR-5 — RLS + schema invariants for the 5 new tables.

Scope:
  - tenant_quotas
  - tenant_billing
  - tenant_invoices (+ invoice_sequence)
  - demo_sessions   (+ polaris-internal seed)
  - audience_members
  - tenants lifecycle columns (deleted_at, purge_at, lifecycle CHECK)

Strategy mirrors PR-3/PR-4 integration tests:
  - session-scoped bootstrap applies migrations + bootstraps role passwords +
    opens both pools (with double-init guard).
  - per-test fixture seeds two tenants via service_pool (BYPASSRLS).
  - RLS reads go through anon_pool with `SET LOCAL app.current_tenant_id` so
    we exercise the same code path the FastAPI TenantContext takes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

os.environ.setdefault("JWT_SECRET", "pytest-secret-32-bytes-padded-xxxxxxxxxx")
os.environ.setdefault("JWT_ISSUER", "polaris-portal")
os.environ.setdefault(
    "POLARIS_KMS_KEY",
    "00112233445566778899aabbccddeeff" "ffeeddccbbaa99887766554433221100",
)

from polaris_api.config import get_settings  # noqa: E402
from polaris_api.db import pools as pools_module  # noqa: E402

# Fixed UUID for the polaris-internal tenant seeded by migration 008b.
# Mirrors the literal in the migration.
_POLARIS_INTERNAL_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def _superuser_dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest_asyncio.fixture(scope="session")
async def pr5_bootstrap() -> AsyncIterator[None]:
    """Apply migrations + open pools once per session."""
    dsn = _superuser_dsn()
    if not dsn:
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL")

    repo_root = Path(__file__).resolve().parents[3]  # noqa: ASYNC240
    apply_script = repo_root / "packages" / "db-migrations" / "apply.py"
    env = {**os.environ, "POLARIS_DATABASE_URL": dsn}
    proc = subprocess.run(  # noqa: S603, ASYNC221 — session-setup; matches `make migrate`
        [sys.executable, str(apply_script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"migration apply failed: {proc.stderr or proc.stdout}")

    test_pw_anon = "anon_pytest_pw"
    test_pw_service = "svc_pytest_pw"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(  # noqa: S608
            f"ALTER ROLE polaris_anon    WITH PASSWORD '{test_pw_anon}'"
        )
        await conn.execute(  # noqa: S608
            f"ALTER ROLE polaris_service WITH PASSWORD '{test_pw_service}'"
        )
    finally:
        await conn.close()

    parsed = urlparse(dsn)
    host_part = parsed.hostname or "localhost"
    if parsed.port:
        host_part = f"{host_part}:{parsed.port}"
    anon_dsn = urlunparse(
        parsed._replace(netloc=f"polaris_anon:{test_pw_anon}@{host_part}")
    )
    service_dsn = urlunparse(
        parsed._replace(netloc=f"polaris_service:{test_pw_service}@{host_part}")
    )

    os.environ["POLARIS_DB_URL_ANON"] = anon_dsn
    os.environ["POLARIS_DB_URL_SERVICE"] = service_dsn
    get_settings.cache_clear()

    if pools_module._anon_pool is None and pools_module._service_pool is None:  # noqa: SLF001
        await pools_module.init_pools()
        opened_here = True
    else:
        opened_here = False
    try:
        yield
    finally:
        if opened_here:
            await pools_module.close_pools()
        get_settings.cache_clear()


def _superuser_dsn() -> str | None:
    """CI/local superuser DSN (POSTGRES_USER=polaris) — bypasses FORCE RLS and
    the table GRANT model. Mirrors test_audit_chain.py::_superuser_dsn."""
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


async def _superuser_cleanup(*statements: tuple[str, tuple[object, ...]]) -> None:
    """Run teardown DELETEs as the superuser.

    Migration 101 REVOKEd DELETE on demo_sessions / tenant_invoices /
    audience_members from polaris_service (hard-delete is funnelled through the
    ``admin_hard_delete_tenant`` SECURITY DEFINER fn per ADR-030), so the
    BYPASSRLS service pool can no longer delete those tables directly. The
    superuser connection bypasses both the REVOKE and FORCE RLS — the correct
    test-cleanup path, matching how test_audit_chain.py handles the migration-012
    audit_logs REVOKE. No-op when no DSN is configured (RLS tests skip anyway).
    """
    dsn = _superuser_dsn()
    if not dsn:
        return
    su = await asyncpg.connect(dsn)
    try:
        for stmt, args in statements:
            await su.execute(stmt, *args)
    finally:
        await su.close()


@pytest_asyncio.fixture
async def two_tenants(pr5_bootstrap: None) -> AsyncIterator[tuple[UUID, UUID]]:
    """Seed two active tenants via service_pool; cleanup at teardown."""
    del pr5_bootstrap
    service_pool = pools_module.get_service_pool()
    suffix = f"{time.time_ns()}-{uuid4().hex[:6]}"
    slug_a = f"pytest-pr5-a-{suffix}"
    slug_b = f"pytest-pr5-b-{suffix}"
    async with service_pool.acquire() as conn:
        a = await conn.fetchval(
            "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug_a
        )
        b = await conn.fetchval(
            "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug_b
        )
    try:
        yield a, b
    finally:
        # Cascade cleanup: FK ON DELETE CASCADE on tenant_quotas + tenant_billing
        # handles those; the others need explicit DELETE-then-tenant. Runs as the
        # superuser — polaris_service lost DELETE on tenant_invoices/demo_sessions/
        # audience_members in migration 101 (hard-delete goes through the
        # admin_hard_delete_tenant SECURITY DEFINER fn).
        await _superuser_cleanup(
            ("DELETE FROM tenant_invoices  WHERE tenant_id IN ($1, $2)", (a, b)),
            ("DELETE FROM demo_sessions    WHERE tenant_id IN ($1, $2)", (a, b)),
            ("DELETE FROM audience_members WHERE tenant_id IN ($1, $2)", (a, b)),
            ("DELETE FROM tenants WHERE id IN ($1, $2)", (a, b)),
        )


async def _seed_quota_row(
    conn: asyncpg.Connection, tenant_id: UUID, bot_replies: int = 5000
) -> None:
    """Insert a minimal tenant_quotas row. Real seeding happens in Phase 6
    via the admin provisioning flow; this is enough for RLS tests."""
    await conn.execute(
        """
        INSERT INTO tenant_quotas (
          tenant_id, bot_replies, conversation_retention_days,
          allowed_channel_kinds, outbound_broadcast_enabled, multi_branch_enabled,
          knowledge_chunks_total_mb, rate_limit_per_minute,
          monthly_llm_tokens_in, monthly_llm_tokens_out,
          monthly_ingestion_pages, monthly_broadcast_recipients,
          monthly_bookings, bot_replies_period_cap
        )
        VALUES ($1, $2, 90, ARRAY['whatsapp'], false, false,
                500, 60, 1000000, 500000, 200, 0, 0, $2)
        """,
        tenant_id,
        bot_replies,
    )


async def _seed_billing_row(
    conn: asyncpg.Connection, tenant_id: UUID, plan_tier: str = "starter"
) -> None:
    await conn.execute(
        "INSERT INTO tenant_billing (tenant_id, plan_tier) VALUES ($1, $2)",
        tenant_id,
        plan_tier,
    )


# ----------------------------------------------------------------------------
# Schema sanity — polaris-internal seed + GENERATED columns + lifecycle CHECK
# ----------------------------------------------------------------------------


async def test_polaris_internal_tenant_was_seeded(pr5_bootstrap: None) -> None:
    """Migration 008b INSERTs the singleton platform tenant. The fixed UUID
    must be reachable so demo handlers can set app.current_tenant_id to it."""
    del pr5_bootstrap
    async with pools_module.get_service_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT slug, status FROM tenants WHERE id = $1",
            _POLARIS_INTERNAL_TENANT_ID,
        )
    assert row is not None, "polaris-internal tenant missing"
    assert row["slug"] == "polaris-internal"
    assert row["status"] == "active"


async def test_demo_sessions_auto_purge_at_value_is_correct(
    pr5_bootstrap: None,
) -> None:
    """Same as above but inspects the value rather than relying on
    transaction rollback semantics. Uses cleanup at end."""
    del pr5_bootstrap
    expires = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    handle = f"pytest-handle-{uuid4().hex[:6]}"
    code = f"DEMO{uuid4().hex[:4].upper()}"
    async with pools_module.get_service_pool().acquire() as conn:
        new_id = await conn.fetchval(
            """
            INSERT INTO demo_sessions
              (tenant_id, channel, channel_user_id, demo_code, expires_at)
            VALUES ($1, 'whatsapp', $2, $3, $4)
            RETURNING id
            """,
            _POLARIS_INTERNAL_TENANT_ID,
            handle,
            code,
            expires,
        )
        try:
            row = await conn.fetchrow(
                "SELECT auto_purge_at FROM demo_sessions WHERE id = $1", new_id
            )
            assert row is not None
            assert row["auto_purge_at"] == expires + timedelta(days=90)
        finally:
            # polaris_service lost DELETE on demo_sessions in migration 101;
            # clean up as the superuser instead of via the service pool.
            await _superuser_cleanup(
                ("DELETE FROM demo_sessions WHERE id = $1", (new_id,)),
            )


async def test_audience_partial_unique_blocks_active_duplicates(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """Two active rows for (tenant, channel, channel_user_id) must collide."""
    tenant_a, _ = two_tenants
    async with pools_module.get_service_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO audience_members "
            "(tenant_id, channel, channel_user_id, opt_in_source) "
            "VALUES ($1, 'whatsapp', '60123456789', 'csv_upload')",
            tenant_a,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO audience_members "
                "(tenant_id, channel, channel_user_id, opt_in_source) "
                "VALUES ($1, 'whatsapp', '60123456789', 'csv_upload')",
                tenant_a,
            )


async def test_audience_partial_unique_allows_reentry_after_optout(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """An opted-out row + a fresh active row for the same (tenant, channel,
    handle) is legal — the partial index excludes opted_out rows."""
    tenant_a, _ = two_tenants
    handle = "60198765432"
    async with pools_module.get_service_pool().acquire() as conn:
        # First membership — opt out immediately.
        await conn.execute(
            """
            INSERT INTO audience_members
              (tenant_id, channel, channel_user_id, opt_in_source,
               opted_out_at, opted_out_reason, opted_out_at_channel)
            VALUES ($1, 'whatsapp', $2, 'csv_upload',
                    now(), 'stop_keyword', 'whatsapp:pol-demo')
            """,
            tenant_a,
            handle,
        )
        # Second membership — active. Must succeed.
        await conn.execute(
            "INSERT INTO audience_members "
            "(tenant_id, channel, channel_user_id, opt_in_source) "
            "VALUES ($1, 'whatsapp', $2, 'api_sync')",
            tenant_a,
            handle,
        )
        # Sanity: exactly two rows for this (tenant, handle).
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM audience_members "
            "WHERE tenant_id = $1 AND channel_user_id = $2",
            tenant_a,
            handle,
        )
        assert count == 2


async def test_lifecycle_check_blocks_inconsistent_deleted_state(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """status='deleted' without deleted_at + purge_at must violate the CHECK."""
    tenant_a, _ = two_tenants
    async with pools_module.get_service_pool().acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE tenants SET status = 'deleted' WHERE id = $1", tenant_a
            )


async def test_lifecycle_check_passes_with_complete_deleted_state(
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, _ = two_tenants
    now = datetime.now(UTC)
    purge = now + timedelta(days=30)
    async with pools_module.get_service_pool().acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET status = 'deleted', deleted_at = $2, purge_at = $3 "
            "WHERE id = $1",
            tenant_a,
            now,
            purge,
        )
        row = await conn.fetchrow(
            "SELECT status, deleted_at, purge_at FROM tenants WHERE id = $1",
            tenant_a,
        )
        assert row is not None
        assert row["status"] == "deleted"
        assert row["deleted_at"] == now
        assert row["purge_at"] == purge


async def test_invoice_number_format_check(two_tenants: tuple[UUID, UUID]) -> None:
    """tenant_invoices.invoice_number must match POL-{M|A}-YYMMINDEX or fail CHECK."""
    tenant_a, _ = two_tenants
    async with pools_module.get_service_pool().acquire() as conn:
        # Valid format passes.
        await conn.execute(
            """
            INSERT INTO tenant_invoices (tenant_id, invoice_number, period, amount_myr)
            VALUES ($1, 'POL-M-26050001', '2026-05', 1099.00)
            """,
            tenant_a,
        )
        # Invalid format fails.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO tenant_invoices (tenant_id, invoice_number, period, amount_myr)
                VALUES ($1, 'BAD-FORMAT', '2026-05', 100.00)
                """,
                tenant_a,
            )


async def test_invoice_sequence_upsert_increments(pr5_bootstrap: None) -> None:
    """The (invoice_type, yy, mm) PK supports the standard upsert-and-increment
    pattern used by the invoice generator."""
    del pr5_bootstrap
    async with pools_module.get_service_pool().acquire() as conn:
        # Clear any prior test data for this slot.
        await conn.execute(
            "DELETE FROM invoice_sequence WHERE invoice_type = 'M' AND yy = 99 AND mm = 12"
        )
        next1 = await conn.fetchval(
            """
            INSERT INTO invoice_sequence (invoice_type, yy, mm, next_index)
            VALUES ('M', 99, 12, 1)
            ON CONFLICT (invoice_type, yy, mm)
            DO UPDATE SET next_index = invoice_sequence.next_index + 1
            RETURNING next_index
            """
        )
        next2 = await conn.fetchval(
            """
            INSERT INTO invoice_sequence (invoice_type, yy, mm, next_index)
            VALUES ('M', 99, 12, 1)
            ON CONFLICT (invoice_type, yy, mm)
            DO UPDATE SET next_index = invoice_sequence.next_index + 1
            RETURNING next_index
            """
        )
        assert next1 == 1
        assert next2 == 2
        # Cleanup.
        await conn.execute(
            "DELETE FROM invoice_sequence WHERE invoice_type = 'M' AND yy = 99 AND mm = 12"
        )


# ----------------------------------------------------------------------------
# RLS — cross-tenant invisibility on each new table
# ----------------------------------------------------------------------------


async def _anon_select(
    tenant_session: UUID, sql: str, *args: object
) -> list[asyncpg.Record]:
    """Helper: run a SELECT through anon_pool with the given session tenant."""
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_session)
        )
        return await conn.fetch(sql, *args)


async def test_rls_tenant_quotas_cross_tenant_returns_zero(
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    async with pools_module.get_service_pool().acquire() as conn:
        await _seed_quota_row(conn, tenant_a)
        await _seed_quota_row(conn, tenant_b)

    cross = await _anon_select(
        tenant_a, "SELECT tenant_id FROM tenant_quotas WHERE tenant_id = $1", tenant_b
    )
    own = await _anon_select(
        tenant_a, "SELECT tenant_id FROM tenant_quotas WHERE tenant_id = $1", tenant_a
    )
    assert cross == []
    assert len(own) == 1


async def test_rls_tenant_billing_cross_tenant_returns_zero(
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    async with pools_module.get_service_pool().acquire() as conn:
        await _seed_billing_row(conn, tenant_a)
        await _seed_billing_row(conn, tenant_b, plan_tier="pro")

    cross = await _anon_select(
        tenant_a, "SELECT plan_tier FROM tenant_billing WHERE tenant_id = $1", tenant_b
    )
    own = await _anon_select(
        tenant_a, "SELECT plan_tier FROM tenant_billing WHERE tenant_id = $1", tenant_a
    )
    assert cross == []
    assert len(own) == 1


async def test_rls_tenant_invoices_cross_tenant_returns_zero(
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    async with pools_module.get_service_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_invoices (tenant_id, invoice_number, period, amount_myr) "
            "VALUES ($1, 'POL-M-26050100', '2026-05', 100.00)",
            tenant_a,
        )
        await conn.execute(
            "INSERT INTO tenant_invoices (tenant_id, invoice_number, period, amount_myr) "
            "VALUES ($1, 'POL-M-26050101', '2026-05', 200.00)",
            tenant_b,
        )

    cross = await _anon_select(
        tenant_a, "SELECT invoice_number FROM tenant_invoices WHERE tenant_id = $1", tenant_b
    )
    own = await _anon_select(
        tenant_a, "SELECT invoice_number FROM tenant_invoices WHERE tenant_id = $1", tenant_a
    )
    assert cross == []
    assert len(own) == 1


async def test_rls_audience_members_cross_tenant_returns_zero(
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    async with pools_module.get_service_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO audience_members (tenant_id, channel, channel_user_id, opt_in_source) "
            "VALUES ($1, 'whatsapp', '60111111111', 'csv_upload')",
            tenant_a,
        )
        await conn.execute(
            "INSERT INTO audience_members (tenant_id, channel, channel_user_id, opt_in_source) "
            "VALUES ($1, 'whatsapp', '60222222222', 'csv_upload')",
            tenant_b,
        )

    cross = await _anon_select(
        tenant_a,
        "SELECT channel_user_id FROM audience_members WHERE tenant_id = $1",
        tenant_b,
    )
    own = await _anon_select(
        tenant_a,
        "SELECT channel_user_id FROM audience_members WHERE tenant_id = $1",
        tenant_a,
    )
    assert cross == []
    assert len(own) == 1


async def test_rls_audience_cross_tenant_insert_blocked(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """WITH CHECK on the FOR ALL policy must block cross-tenant INSERT."""
    tenant_a, tenant_b = two_tenants
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO audience_members "
                "(tenant_id, channel, channel_user_id, opt_in_source) "
                "VALUES ($1, 'whatsapp', '60333333333', 'csv_upload')",
                tenant_b,
            )


async def test_rls_demo_sessions_cross_tenant_returns_zero(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """demo_sessions is tenant_id-scoped; tenant A's session can't see
    tenant B's demo rows. (In practice all demo rows live under
    polaris-internal, but the policy must still apply per-tenant.)"""
    tenant_a, tenant_b = two_tenants
    expires = datetime.now(UTC) + timedelta(hours=6)
    async with pools_module.get_service_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO demo_sessions "
            "(tenant_id, channel, channel_user_id, demo_code, expires_at) "
            "VALUES ($1, 'whatsapp', '60111111111', 'AAAA', $2)",
            tenant_a,
            expires,
        )
        await conn.execute(
            "INSERT INTO demo_sessions "
            "(tenant_id, channel, channel_user_id, demo_code, expires_at) "
            "VALUES ($1, 'whatsapp', '60222222222', 'BBBB', $2)",
            tenant_b,
            expires,
        )
    cross = await _anon_select(
        tenant_a,
        "SELECT channel_user_id FROM demo_sessions WHERE tenant_id = $1",
        tenant_b,
    )
    own = await _anon_select(
        tenant_a,
        "SELECT channel_user_id FROM demo_sessions WHERE tenant_id = $1",
        tenant_a,
    )
    assert cross == []
    assert len(own) == 1


async def test_anon_has_no_access_to_invoice_sequence(pr5_bootstrap: None) -> None:
    """invoice_sequence is platform-only — polaris_anon must get a permission
    error on SELECT."""
    del pr5_bootstrap
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM invoice_sequence")


async def test_force_rls_enabled_on_all_pr5_tables(pr5_bootstrap: None) -> None:
    """Belt + braces: every new RLS-scoped table has both ENABLE and FORCE.

    invoice_sequence is platform-only (no RLS by design), so excluded.
    """
    del pr5_bootstrap
    async with pools_module.get_service_pool().acquire() as conn:
        for table in (
            "tenant_quotas",
            "tenant_billing",
            "tenant_invoices",
            "demo_sessions",
            "audience_members",
        ):
            row = await conn.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE oid = ('public.' || $1)::regclass",
                table,
            )
            assert row is not None
            assert row["relrowsecurity"], f"{table}: RLS not enabled"
            assert row["relforcerowsecurity"], f"{table}: FORCE RLS not set"


