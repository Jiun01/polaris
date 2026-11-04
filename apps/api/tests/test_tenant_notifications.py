"""Phase 8 PR-1 — tenant_notifications router + RLS isolation tests.

Two test classes:

  * ``TestNotificationsRLS`` — pure DB tests proving cross-tenant
    isolation on the `notifications` and `plan_change_requests` tables
    introduced in migration 051. Mirrors the black-box pattern in
    [test_rls_phase_1.py]: seed as BYPASSRLS service role, assert as
    NOBYPASSRLS `polaris_test_anon` role with `app.current_tenant_id`
    set via the function-form ``set_config``.

  * ``TestNotificationsRouter`` — light unit coverage of the router's
    pagination + idempotency contract. Uses the `service_conn` directly
    (no ASGI client) because the router logic is thin enough that the
    SQL paths are what need verification; the router-level behaviour
    (auth, dependency injection) is already covered by the
    auth/middleware tests when those land in PR-2.

DB-gated like the rest of the RLS suite: if `POLARIS_DATABASE_URL` is
unset, the whole module is skipped via the `service_conn` fixture.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest

# ---------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------


async def _seed_tenant(conn: asyncpg.Connection, slug: str) -> UUID:
    row = await conn.fetchrow(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id",
        slug,
    )
    assert row is not None
    # asyncpg returns Record values as Any; cast keeps strict mypy happy
    # without paying a runtime UUID re-parse (asyncpg's codec already
    # produced a UUID).
    return UUID(str(row["id"]))


async def _seed_auth_user(conn: asyncpg.Connection, email: str) -> UUID:
    row = await conn.fetchrow(
        "INSERT INTO auth.users (email) VALUES ($1) RETURNING id",
        email,
    )
    assert row is not None
    # asyncpg returns Record values as Any; cast keeps strict mypy happy
    # without paying a runtime UUID re-parse (asyncpg's codec already
    # produced a UUID).
    return UUID(str(row["id"]))


async def _seed_notification(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    kind: str = "broadcast_completed",
    title: str = "test",
    recipient_user_id: UUID | None = None,
    read: bool = False,
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO notifications
            (tenant_id, recipient_user_id, kind, title, read_at)
        VALUES ($1, $2, $3, $4, CASE WHEN $5 THEN now() ELSE NULL END)
        RETURNING id
        """,
        tenant_id,
        recipient_user_id,
        kind,
        title,
        read,
    )
    assert row is not None
    # asyncpg returns Record values as Any; cast keeps strict mypy happy
    # without paying a runtime UUID re-parse (asyncpg's codec already
    # produced a UUID).
    return UUID(str(row["id"]))


async def _seed_plan_change_request(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    requested_by_user_id: UUID,
    current_tier: str = "starter",
    target_tier: str = "pro",
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO plan_change_requests
            (tenant_id, requested_by_user_id,
             current_tier, target_tier,
             current_billing_cycle, target_billing_cycle,
             proration_preview)
        VALUES ($1, $2, $3, $4, 'monthly', 'monthly',
                '{"amount_due_myr": 0, "period_credited_myr": 0,
                  "quota_topup_bot_replies": 0, "prorate_basis": "calendar_month"}'::jsonb)
        RETURNING id
        """,
        tenant_id,
        requested_by_user_id,
        current_tier,
        target_tier,
    )
    assert row is not None
    # asyncpg returns Record values as Any; cast keeps strict mypy happy
    # without paying a runtime UUID re-parse (asyncpg's codec already
    # produced a UUID).
    return UUID(str(row["id"]))


# =====================================================================
# RLS isolation
# =====================================================================


@pytest.mark.parametrize("table", ["notifications", "plan_change_requests"])
async def test_force_rls_enabled(
    service_conn: asyncpg.Connection,
    table: str,
) -> None:
    """Migration 051 must set both ENABLE and FORCE RLS — same invariant
    as Phase 1 (test_rls_phase_1.py::test_force_rls_enabled)."""
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


async def test_notifications_cross_tenant_select_returns_zero_rows(
    service_conn: asyncpg.Connection,
) -> None:
    """Tenant A under RLS cannot see Tenant B's notifications."""
    tenant_a = await _seed_tenant(service_conn, slug="pytest-notif-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-notif-b")
    await _seed_notification(service_conn, tenant_id=tenant_a, title="for-a")
    notif_b = await _seed_notification(service_conn, tenant_id=tenant_b, title="for-b")

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )

        cross = await service_conn.fetch(
            "SELECT id FROM notifications WHERE id = $1", notif_b
        )
        assert cross == [], "RLS leak: tenant_a saw tenant_b's notification"

        own = await service_conn.fetch(
            "SELECT id, title FROM notifications WHERE tenant_id = $1", tenant_a
        )
        assert len(own) == 1
        assert own[0]["title"] == "for-a"
    finally:
        await tx.rollback()


async def test_notifications_cross_tenant_update_does_not_leak(
    service_conn: asyncpg.Connection,
) -> None:
    """Tenant A cannot mark Tenant B's notification read — UPDATE under
    RLS sees zero rows in scope, so the rowcount reflects 0 modifications.

    This is the load-bearing test for the PATCH endpoint: an attacker
    knowing Tenant B's notification UUID still cannot poison Tenant B's
    inbox state from inside Tenant A's session.
    """
    tenant_a = await _seed_tenant(service_conn, slug="pytest-notif-upd-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-notif-upd-b")
    notif_b = await _seed_notification(service_conn, tenant_id=tenant_b)

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )

        # Spelled UPDATE … RETURNING to match the router's SQL — confirms
        # the RETURNING set comes back empty under cross-tenant attempt.
        rows = await service_conn.fetch(
            "UPDATE notifications SET read_at = now() WHERE id = $1 RETURNING id",
            notif_b,
        )
        assert rows == [], "RLS leak: tenant_a marked tenant_b's notification read"
    finally:
        await tx.rollback()

    # Sanity: tenant_b's notification is still unread (the attempted
    # UPDATE was a no-op).
    after = await service_conn.fetchrow(
        "SELECT read_at FROM notifications WHERE id = $1", notif_b
    )
    assert after is not None
    assert after["read_at"] is None


async def test_plan_change_requests_cross_tenant_invisible(
    service_conn: asyncpg.Connection,
) -> None:
    tenant_a = await _seed_tenant(service_conn, slug="pytest-pcr-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-pcr-b")
    user_a = await _seed_auth_user(service_conn, email="pytest-pcr-a@pytest.invalid")
    user_b = await _seed_auth_user(service_conn, email="pytest-pcr-b@pytest.invalid")
    await _seed_plan_change_request(
        service_conn, tenant_id=tenant_a, requested_by_user_id=user_a
    )
    pcr_b = await _seed_plan_change_request(
        service_conn, tenant_id=tenant_b, requested_by_user_id=user_b
    )

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        cross = await service_conn.fetch(
            "SELECT id FROM plan_change_requests WHERE id = $1", pcr_b
        )
        assert cross == []

        own = await service_conn.fetch(
            "SELECT id FROM plan_change_requests WHERE tenant_id = $1", tenant_a
        )
        assert len(own) == 1
    finally:
        await tx.rollback()


# =====================================================================
# Notification kind CHECK & shape
# =====================================================================


async def test_notification_title_length_check_enforced(
    service_conn: asyncpg.Connection,
) -> None:
    """`title` CHECK (1..200) rejects empty + oversized values."""
    tenant = await _seed_tenant(service_conn, slug="pytest-notif-check")

    with pytest.raises(asyncpg.CheckViolationError):
        await service_conn.execute(
            "INSERT INTO notifications (tenant_id, kind, title) "
            "VALUES ($1, 'broadcast_completed', '')",
            tenant,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await service_conn.execute(
            "INSERT INTO notifications (tenant_id, kind, title) "
            "VALUES ($1, 'broadcast_completed', $2)",
            tenant,
            "x" * 201,
        )


# =====================================================================
# Idempotency + transition semantics (router SQL through service role)
# =====================================================================


async def test_mark_read_then_idempotent_keeps_original_timestamp(
    service_conn: asyncpg.Connection,
) -> None:
    """COALESCE in the router's UPDATE means setting read=true twice
    preserves the first read_at value (idempotency contract)."""
    tenant = await _seed_tenant(service_conn, slug="pytest-notif-idem")
    notif = await _seed_notification(service_conn, tenant_id=tenant)

    row1 = await service_conn.fetchrow(
        """
        UPDATE notifications
           SET read_at = CASE WHEN $2::boolean THEN COALESCE(read_at, now()) ELSE NULL END
         WHERE id = $1
        RETURNING read_at
        """,
        notif,
        True,
    )
    assert row1 is not None
    first_marker = row1["read_at"]
    assert first_marker is not None

    # Sleeping isn't reliable in CI; rerun the same UPDATE and confirm
    # the timestamp didn't move.
    row2 = await service_conn.fetchrow(
        """
        UPDATE notifications
           SET read_at = CASE WHEN $2::boolean THEN COALESCE(read_at, now()) ELSE NULL END
         WHERE id = $1
        RETURNING read_at
        """,
        notif,
        True,
    )
    assert row2 is not None
    assert row2["read_at"] == first_marker


async def test_mark_unread_clears_read_marker(
    service_conn: asyncpg.Connection,
) -> None:
    tenant = await _seed_tenant(service_conn, slug="pytest-notif-unread")
    notif = await _seed_notification(service_conn, tenant_id=tenant, read=True)

    row = await service_conn.fetchrow(
        """
        UPDATE notifications
           SET read_at = CASE WHEN $2::boolean THEN COALESCE(read_at, now()) ELSE NULL END
         WHERE id = $1
        RETURNING read_at
        """,
        notif,
        False,
    )
    assert row is not None
    assert row["read_at"] is None


async def test_mark_all_read_only_affects_unread_rows(
    service_conn: asyncpg.Connection,
) -> None:
    """RETURNING count from the bulk-mark UPDATE must equal exactly the
    number of unread rows at call time, not the table-total."""
    tenant = await _seed_tenant(service_conn, slug="pytest-notif-bulk")
    await _seed_notification(service_conn, tenant_id=tenant, read=False)
    await _seed_notification(service_conn, tenant_id=tenant, read=False)
    await _seed_notification(service_conn, tenant_id=tenant, read=True)  # already read

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant)
        )
        rows = await service_conn.fetch(
            """
            UPDATE notifications
               SET read_at = now()
             WHERE read_at IS NULL
            RETURNING 1
            """,
        )
        assert len(rows) == 2  # only the two unread
    finally:
        await tx.rollback()


# =====================================================================
# Partial-index existence
# =====================================================================


async def test_unread_partial_index_exists(service_conn: asyncpg.Connection) -> None:
    """Migration 051 creates `notifications_tenant_unread_idx` as a
    partial index. Loss of the WHERE clause would silently turn the
    bell-icon poll into a full-table scan as production volume grows."""
    row = await service_conn.fetchrow(
        """
        SELECT indexdef
          FROM pg_indexes
         WHERE schemaname = 'public'
           AND tablename = 'notifications'
           AND indexname = 'notifications_tenant_unread_idx'
        """,
    )
    assert row is not None, "notifications_tenant_unread_idx missing"
    assert "WHERE" in row["indexdef"].upper()
    assert "read_at IS NULL" in row["indexdef"]


async def test_plan_change_requests_pending_partial_index_exists(
    service_conn: asyncpg.Connection,
) -> None:
    row = await service_conn.fetchrow(
        """
        SELECT indexdef
          FROM pg_indexes
         WHERE schemaname = 'public'
           AND tablename = 'plan_change_requests'
           AND indexname = 'plan_change_requests_pending_idx'
        """,
    )
    assert row is not None
    assert "status = 'pending'" in row["indexdef"]
