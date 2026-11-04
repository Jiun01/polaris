"""Nightly audit chain verification job -- Phase 9 PR-5.

APScheduler cron job: fires at 04:00 Asia/Kuala_Lumpur every night.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_scheduler = None


def register_scheduler(pool: object) -> None:
    """Register nightly 04:00 MYT job."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import (
            AsyncIOScheduler,
        )
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed -- nightly audit chain verification disabled.")
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock
    from polaris_api.services.audit_chain_verifier import run_verification

    async def _run() -> None:
        logger.info("audit_chain_verify: nightly job starting")
        result = await run_verification(
            triggered_by="scheduled",
            triggered_by_admin_id=None,
            pool=pool,
        )
        logger.info(
            "audit_chain_verify: complete status=%s rows=%s duration_ms=%s",
            result["status"], result["rows_scanned"], result["duration_ms"],
        )
        if result["status"] == "broken":
            logger.error(
                "AUDIT CHAIN BROKEN at row %s -- investigate immediately",
                result.get("broken_at_id"),
            )

    async def _nightly_job() -> None:
        await run_with_advisory_lock(
            pool,
            job_name="audit_chain_verify",
            lock_key=JOB_LOCK_KEYS["audit_chain_verify"],
            job=_run,
        )

    _scheduler = AsyncIOScheduler(timezone="Asia/Kuala_Lumpur")
    _scheduler.add_job(
        _nightly_job,
        CronTrigger(hour=4, minute=0, timezone="Asia/Kuala_Lumpur"),
        id="audit_chain_verify_nightly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("audit_chain_verify: scheduled at 04:00 MYT")


def shutdown_scheduler() -> None:
    """Stop the scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
