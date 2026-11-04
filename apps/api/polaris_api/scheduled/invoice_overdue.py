from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from polaris_api.services.audit import record as audit_record

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)
_scheduler = None


async def _flip_overdue_invoices(conn: asyncpg.Connection) -> int:
    """Flip every ``issued`` invoice past its due date to ``overdue``.

    audit Batch B (B7-e): ONE bulk ``UPDATE ... RETURNING`` flips every
    invoice in a single statement — replacing the previous per-row
    UPDATE + txn loop. The per-row ``audit_record`` calls stay sequential
    AFTER the bulk flip: each row's hash depends on the previous row's
    hash under a per-tenant advisory lock (``services/audit.py``), so
    batching those via UNNEST/executemany would break the chain the
    nightly ``audit_chain_verify`` job asserts. Returns the flip count.
    """
    rows = await conn.fetch(
        "UPDATE tenant_invoices "
        "   SET status = 'overdue', updated_at = NOW() "
        " WHERE status = 'issued' AND due_at < NOW() "
        "RETURNING id, tenant_id"
    )
    for row in rows:
        # Audit rows MUST stay sequential — per-tenant hash chain
        # (services/audit.py's record() takes a per-tenant advisory lock
        # and chains each row's hash off the previous one). Each call needs
        # its own transaction: pg_advisory_xact_lock is transaction-scoped,
        # so in autocommit the lock would release after its own statement
        # and no longer serialise the prev_hash read + insert against
        # concurrent audit writers for the same tenant.
        async with conn.transaction():
            await audit_record(
                conn,
                tenant_id=row["tenant_id"],
                actor=None,
                action="invoice.overdue",
                before={"status": "issued"},
                after={"status": "overdue", "invoice_id": str(row["id"])},
            )
    return len(rows)


def register_overdue_scheduler(pool: object) -> None:
    """Register daily 09:00 MYT job to flip overdue invoices."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed -- invoice overdue scheduler disabled.")
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock

    async def _run() -> None:
        logger.info("invoice_overdue: daily job starting")
        try:
            async with pool.acquire() as conn:  # type: ignore[attr-defined]
                flipped = await _flip_overdue_invoices(conn)
                logger.info("invoice_overdue: flipped %s invoices to overdue", flipped)
        except Exception as exc:
            logger.error("invoice_overdue: job failed err=%s", exc, exc_info=True)

    async def _daily_job() -> None:
        await run_with_advisory_lock(
            pool,
            job_name="invoice_overdue",
            lock_key=JOB_LOCK_KEYS["invoice_overdue"],
            job=_run,
        )

    _scheduler = AsyncIOScheduler(timezone="Asia/Kuala_Lumpur")
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=9, minute=0, timezone="Asia/Kuala_Lumpur"),
        id="invoice_overdue_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("invoice_overdue: scheduled at 09:00 MYT")


def shutdown_overdue_scheduler() -> None:
    """Stop the overdue scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
