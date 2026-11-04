"""MRR rollup APScheduler cron job — Phase 9 PR-11.

Registered at 02:00 Asia/Kuala_Lumpur nightly.
Calls mrr_rollup.compute_and_persist() which handles:
  - SUM of tenant_billing.mrr_myr
  - alert_band computation
  - 30-entry in-memory trend ring-buffer
  - one-shot Sentry alert on band crossing
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_scheduler = None


def register_mrr_scheduler(pool: object) -> None:
    """Register nightly 02:00 MYT job to compute and persist MRR rollup."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed -- MRR rollup scheduler disabled.")
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock
    from polaris_api.services import mrr_rollup

    async def _run() -> None:
        logger.info("mrr_rollup_job: nightly run starting")
        try:
            async with pool.acquire() as conn:  # type: ignore[attr-defined]
                result = await mrr_rollup.compute_and_persist(conn)
            logger.info(
                "mrr_rollup_job: complete mrr_myr=%.2f band=%s",
                result["mrr_myr"],
                result["alert_band"],
            )
        except Exception as exc:
            logger.error("mrr_rollup_job: failed err=%s", exc, exc_info=True)

    async def _nightly_job() -> None:
        await run_with_advisory_lock(
            pool,
            job_name="mrr_rollup",
            lock_key=JOB_LOCK_KEYS["mrr_rollup"],
            job=_run,
        )

    _scheduler = AsyncIOScheduler(timezone="Asia/Kuala_Lumpur")
    _scheduler.add_job(
        _nightly_job,
        CronTrigger(hour=2, minute=0, timezone="Asia/Kuala_Lumpur"),
        id="mrr_rollup_nightly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("mrr_rollup_job: scheduled at 02:00 MYT")


def shutdown_mrr_scheduler() -> None:
    """Stop the MRR rollup scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
