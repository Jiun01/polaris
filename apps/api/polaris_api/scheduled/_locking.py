"""Cross-process locking for scheduled cron jobs — phase-9 hardening.

Every nightly job runs inside the FastAPI web process via APScheduler. Today
the API runs as a single process (one uvicorn worker, one container), so only
one scheduler exists and double-firing cannot happen. The moment the API is
scaled to multiple workers (`uvicorn --workers N`) or replicas, *each* process
spawns its own APScheduler and fires the same cron job at the same minute —
e.g. two `invoice.overdue` flips, two MRR snapshots, two retention sweeps.

`run_with_advisory_lock` guards against that with a Postgres **session-level
advisory lock** (`pg_try_advisory_lock`). The lock is cluster-wide, so it works
across processes *and* replicas pointing at the same database. Exactly one
instance acquires the lock and runs the job; the others no-op for that tick.

The lock is held on a dedicated service-pool connection for the whole job and
released in a `finally`. If the holder crashes mid-job, Postgres drops the
session and the lock with it, so the next tick is free to run (at-least-once,
never concurrent).

Lock keys are fixed per job (`JOB_LOCK_KEYS`). They are arbitrary but stable
and unique; the high base offset keeps them clear of any app-level advisory
locks added later.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Fixed advisory-lock keys per scheduled job. Stable + unique; do not reuse.
JOB_LOCK_KEYS: dict[str, int] = {
    "quota_flush": 9_110_001,
    "mrr_rollup": 9_110_002,
    "retention_sweep": 9_110_003,
    "cost_ceiling_check": 9_110_004,
    "audit_chain_verify": 9_110_005,
    "invoice_overdue": 9_110_006,
    "weekly_digest": 9_110_007,
    "ea_settings_sync": 9_110_008,
    "reconcile_channels": 9_110_009,
}


async def run_with_advisory_lock(
    pool: Any,
    *,
    job_name: str,
    lock_key: int,
    job: Callable[[], Awaitable[None]],
) -> None:
    """Run ``job`` iff this process can acquire the advisory lock ``lock_key``.

    Acquires a session-level ``pg_try_advisory_lock`` on a dedicated connection
    from ``pool``. If the lock is already held (another process/replica is
    running this job), logs and returns without executing. Always releases the
    lock in ``finally``.

    Args:
        pool: An asyncpg pool (service pool — BYPASSRLS).
        job_name: Human-readable job name for logs.
        lock_key: The fixed advisory-lock key for this job (see JOB_LOCK_KEYS).
        job: Zero-arg coroutine factory doing the actual work.
    """
    async with pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
        if not locked:
            logger.info(
                "scheduled job %s: advisory lock %s held by another instance; "
                "skipping this tick",
                job_name,
                lock_key,
            )
            return
        try:
            await job()
        finally:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
            except Exception:  # noqa: BLE001 — session reset on release drops it anyway
                logger.warning(
                    "scheduled job %s: advisory unlock failed; session reset will "
                    "release lock %s",
                    job_name,
                    lock_key,
                )
