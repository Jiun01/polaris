"""Nightly retention sweeper + DSAR grace-period purge — Phase 10 PR-3.

APScheduler cron: 02:00 MYT (UTC+8).  Runs after quota_flush (00:05)
and audit_chain_verify (04:00) so all three share the nightly window
without overlap.

Two passes per run:

1. **Retention sweep** (active + suspended tenants):
   Hard-delete rows that exceed the tenant's contractual retention
   window, sourced from ``tenant_quotas``:

   - ``messages``             — beyond ``conversation_retention_days``
   - ``ingestion_requests``   — beyond ``ingestion_requests_retention_days``

   ``audit_logs`` is intentionally NOT swept: it is an append-only,
   hash-chained ledger (ADR-030), so ``audit_log_retention_days`` is
   retained but never enforced (migration 103 sets it to 0). See
   ``_sweep_tenant`` for the rationale.

   A ``retention.sweep`` audit row is emitted per tenant per run so the
   hash-chain verifier can confirm the sweep happened.

   Columns with value ``0`` skip deletion for that table (``0 = never``).

2. **DSAR grace-period purge** (deleted tenants):
   Tenants whose ``status = 'deleted'`` and whose ``updated_at`` is
   ≥ 30 days ago are physically purged in cascade order per ADR-029:

       messages → knowledge_chunks → audience_members →
       tenant_channels → tenant_users → tenants

   Each cascade step is logged; failure of one step is retried on the
   next run (the tenant row stays deleted until the full cascade passes).

Per-tenant error isolation — one bad tenant never aborts the whole job.

ADR refs: ADR-029 (30-day grace), ADR-027 (demo data window).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from polaris_api.services.audit import record as audit_record
from polaris_api.services.demo_sessions import POLARIS_INTERNAL_TENANT_ID

logger = logging.getLogger(__name__)

_scheduler = None

# Cascade order for DSAR physical purge (ADR-029).
_DSAR_CASCADE_TABLES = (
    "messages",
    "knowledge_chunks",
    "audience_members",
    "tenant_channels",
    "tenant_users",
)
_GRACE_DAYS = 30

# audit Batch B (B7-b): cap per-statement DELETE size so a tenant with a huge
# backlog (first sweep after a long-idle retention setting, or a bulk DSAR
# purge) never holds one service-pool conn + a long-running DELETE for the
# whole row count. Each batch is its own statement; the loop below re-issues
# until a batch comes back short (< _DELETE_BATCH rows), which means the
# table is caught up.
_DELETE_BATCH = 10_000


# ── main job ──────────────────────────────────────────────────────────────────


async def _sweep_job(service_pool: asyncpg.Pool) -> None:
    """One full sweep pass — retention + DSAR purge."""
    run_start = datetime.now(UTC)
    logger.info("retention_sweep: run started at %s", run_start.isoformat())
    swept = errors = purged = 0

    # audit Batch B (B7-b): snapshot the tenant list on a short-lived conn,
    # then RELEASE it — mirrors ea_settings_sync.py's Pattern 4. The per-
    # tenant loop below acquires a FRESH conn per tenant so a service-pool
    # slot is never held for the whole nightly sweep (previously one conn
    # was held for every tenant + every DELETE, for the entire run).
    async with service_pool.acquire() as conn:
        tenants = await conn.fetch(
            """
            SELECT
                t.id                                        AS tenant_id,
                t.status,
                t.updated_at                               AS status_updated_at,
                tq.conversation_retention_days,
                tq.audit_log_retention_days,
                tq.ingestion_requests_retention_days
            FROM tenants t
            LEFT JOIN tenant_quotas tq ON tq.tenant_id = t.id
            WHERE t.status IN ('active', 'suspended', 'deleted')
            ORDER BY t.created_at
            """
        )

    for row in tenants:
        tenant_id: UUID = row["tenant_id"]
        try:
            # Fresh connection per tenant so the slot is released between
            # tenants (audit Batch B / B7-b).
            async with service_pool.acquire() as conn:
                if row["status"] == "deleted":
                    await _maybe_purge(conn, tenant_id, row["status_updated_at"], run_start)
                    purged += 1
                else:
                    await _sweep_tenant(conn, tenant_id, row, run_start)
                    swept += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "retention_sweep: error tenant=%s: %s — skipping",
                tenant_id,
                exc,
            )
            errors += 1

    elapsed = (datetime.now(UTC) - run_start).total_seconds()
    logger.info(
        "retention_sweep: done swept=%d purged=%d errors=%d elapsed_s=%.1f",
        swept,
        purged,
        errors,
        elapsed,
    )


# ── retention sweep ───────────────────────────────────────────────────────────


async def _chunked_age_delete(
    conn: asyncpg.Connection,
    *,
    table: str,
    tenant_id: UUID,
    retention_days: int,
) -> int:
    """DELETE rows older than ``retention_days`` in ``_DELETE_BATCH``-row
    batches (audit Batch B / B7-b).

    Table name is NEVER user input — always one of a fixed, hardcoded set of
    call sites in this module (``messages`` / ``ingestion_requests``), so the
    f-string is safe (mirrors the existing ``# noqa: S608`` posture on the
    DSAR cascade DELETE below). Returns the total rows deleted, for logging.
    """
    total = 0
    while True:
        tag = await conn.execute(
            f"DELETE FROM {table} WHERE ctid IN ("  # noqa: S608
            f"  SELECT ctid FROM {table}"
            f"   WHERE tenant_id = $1"
            f"     AND created_at < now() - ($2 * interval '1 day')"
            f"   LIMIT {_DELETE_BATCH}"
            f")",
            tenant_id,
            retention_days,
        )
        # asyncpg's execute() returns a command tag like "DELETE 1234".
        deleted = int(tag.split()[-1])
        total += deleted
        if deleted < _DELETE_BATCH:
            break
    return total


async def _sweep_tenant(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    row: asyncpg.Record,
    run_start: datetime,
) -> None:
    """Hard-delete rows beyond retention windows for one active tenant."""
    conv_days: int = row["conversation_retention_days"] or 0
    audit_days: int = row["audit_log_retention_days"] or 0
    ingestion_days: int = row["ingestion_requests_retention_days"] or 0

    if conv_days > 0:
        await _chunked_age_delete(
            conn, table="messages", tenant_id=tenant_id, retention_days=conv_days,
        )

    # audit_logs is intentionally NEVER swept. It is an append-only, hash-chained
    # ledger (ADR-030; migration 012 REVOKEs DELETE from polaris_service, and
    # deleting rows would break the chain verifier in services/audit.py). The
    # audit_log_retention_days column is retained for forward-compat but is not
    # enforced — migration 103 sets it to 0 (= never) so the schema matches. The
    # value is still recorded in the retention.sweep audit row below for
    # transparency. On-request erasure is handled by admin_hard_delete_tenant
    # (SECURITY DEFINER). If age-based audit purge is ever required, that is the
    # deferred "Option C" design (privileged purge fn + chain-verifier rework).

    if ingestion_days > 0:
        await _chunked_age_delete(
            conn,
            table="ingestion_requests",
            tenant_id=tenant_id,
            retention_days=ingestion_days,
        )

    await audit_record(
        conn,
        tenant_id=tenant_id,
        actor=None,
        action="retention.sweep",
        after={
            "run_at": run_start.isoformat(),
            "conversation_retention_days": conv_days,
            "audit_log_retention_days": audit_days,
            "ingestion_requests_retention_days": ingestion_days,
        },
    )


# ── DSAR grace-period physical purge ─────────────────────────────────────────


async def _chunked_tenant_delete(
    conn: asyncpg.Connection, *, table: str, tenant_id: UUID,
) -> int:
    """DELETE all rows for ``tenant_id`` from ``table`` in ``_DELETE_BATCH``
    batches (audit Batch B / B7-b). ``table`` is always one of the fixed
    ``_DSAR_CASCADE_TABLES`` tuple — never user input.
    """
    total = 0
    while True:
        tag = await conn.execute(
            f"DELETE FROM {table} WHERE ctid IN ("  # noqa: S608
            f"  SELECT ctid FROM {table} WHERE tenant_id = $1 LIMIT {_DELETE_BATCH}"
            f")",
            tenant_id,
        )
        deleted = int(tag.split()[-1])
        total += deleted
        if deleted < _DELETE_BATCH:
            break
    return total


async def _maybe_purge(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    status_updated_at: datetime | None,
    run_start: datetime,
) -> None:
    """Physically purge a deleted tenant if the 30-day grace window has passed."""
    if tenant_id == POLARIS_INTERNAL_TENANT_ID:
        logger.warning(
            "retention_sweep: refusing to purge the polaris-internal demo gateway tenant=%s",
            tenant_id,
        )
        return
    if status_updated_at is None:
        logger.warning(
            "retention_sweep: deleted tenant=%s has no status_updated_at — skipping purge",
            tenant_id,
        )
        return

    updated_utc = (
        status_updated_at
        if status_updated_at.tzinfo is not None
        else status_updated_at.replace(tzinfo=UTC)
    )
    grace_expires = updated_utc + timedelta(days=_GRACE_DAYS)
    if run_start < grace_expires:
        logger.debug(
            "retention_sweep: tenant=%s grace expires %s — not yet",
            tenant_id,
            grace_expires.isoformat(),
        )
        return

    logger.info(
        "retention_sweep: purging tenant=%s (grace expired %s)",
        tenant_id,
        grace_expires.isoformat(),
    )
    # audit Batch B (B7-b): chunked per-table DELETEs, autocommit per batch
    # (no wrapping transaction() — was one giant tx spanning every cascade
    # table + the final tenants row). Purge is idempotent + re-runnable: a
    # crash mid-cascade leaves partial deletes, but the tenant row is
    # deleted LAST, so the trigger condition (status='deleted') persists
    # and the next sweep resumes the purge from wherever it left off.
    for table in _DSAR_CASCADE_TABLES:
        await _chunked_tenant_delete(conn, table=table, tenant_id=tenant_id)
    await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    logger.info("retention_sweep: tenant=%s physically purged", tenant_id)


# ── scheduler registration ────────────────────────────────────────────────────


def register_retention_sweep_scheduler(service_pool: asyncpg.Pool) -> None:
    """Register the nightly 02:00 MYT retention sweep job."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed — retention sweep scheduler disabled.")
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock

    async def _job() -> None:
        await run_with_advisory_lock(
            service_pool,
            job_name="retention_sweep",
            lock_key=JOB_LOCK_KEYS["retention_sweep"],
            job=lambda: _sweep_job(service_pool),
        )

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _job,
        CronTrigger(hour=2, minute=0, timezone="Asia/Kuala_Lumpur"),
        id="retention_sweep",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("retention_sweep: scheduler registered (02:00 MYT daily)")


def shutdown_retention_sweep_scheduler() -> None:
    """Gracefully stop the retention sweep scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
