"""Periodic EA → Polaris booking-settings mirror — Phase 8 follow-on.

APScheduler **interval** job (default hourly): re-syncs every active EA tenant's
booking policy + schedule from EA into ``tenants.settings`` so the bot enforces
EA's current values and the portal's "next refresh" countdown stays honest.

Why an interval (not a nightly cron) like the other ``scheduled/`` jobs: the
mirror is the bot's view of bookable hours / lead-time / blocked dates, so a
once-a-day refresh would let a tenant's calendar edits go un-enforced for up to
24h. Tenants can always force an immediate refresh from the portal ("Update bot
knowledge" → ``POST /config/sync``); this tick is the safety net.

Per-tenant error isolation: ``sync_ea_calendar`` never raises, so one
unreachable EA tenant is skipped and the batch continues. Uses the service pool
(BYPASSRLS) to scan across tenants in one query, then syncs each sequentially to
avoid hammering the co-located EA service.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

from polaris_api.services.booking.settings_sync import (
    DEFAULT_REFRESH_INTERVAL_S,
    sync_ea_calendar_pooled,
)

logger = logging.getLogger(__name__)

_scheduler = None


def _interval_seconds() -> int:
    """Refresh cadence from ``EA_SYNC_INTERVAL_SECONDS`` (default 1h)."""
    raw = os.getenv("EA_SYNC_INTERVAL_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_REFRESH_INTERVAL_S
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "EA_SYNC_INTERVAL_SECONDS=%r not int; using default %d",
            raw,
            DEFAULT_REFRESH_INTERVAL_S,
        )
        return DEFAULT_REFRESH_INTERVAL_S
    return value if value > 0 else DEFAULT_REFRESH_INTERVAL_S


async def _sync_job(service_pool: asyncpg.Pool, *, interval_s: int) -> None:
    """One full mirror pass over every active EA tenant."""
    run_start = datetime.now(UTC)
    synced = skipped = 0

    # Snapshot the active-EA tenant ids on a short-lived connection, then RELEASE
    # it before the per-tenant EA HTTP work. Each sync makes two (slow) EA calls;
    # holding one service-pool slot for the whole batch would starve other
    # cross-tenant work for the duration.
    async with service_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id
              FROM tenants
             WHERE status = 'active'
               AND settings->'ea_account'->>'status' = 'active'
             ORDER BY created_at
            """
        )
    tenant_ids: list[UUID] = [row["id"] for row in rows]

    for tenant_id in tenant_ids:
        # audit Batch B (B7-a): sync_ea_calendar_pooled acquires+releases the
        # conn AROUND the two EA HTTP calls (not across them) — the previous
        # per-tenant `async with service_pool.acquire()` held one of the 4
        # service-pool slots for the full EA round-trip on every tenant.
        result = await sync_ea_calendar_pooled(
            service_pool, tenant_id=tenant_id, interval_s=interval_s
        )
        if result.ok:
            synced += 1
        else:
            skipped += 1

    elapsed = (datetime.now(UTC) - run_start).total_seconds()
    logger.info(
        "ea_settings_sync: done synced=%d skipped=%d elapsed_s=%.1f",
        synced,
        skipped,
        elapsed,
    )


def register_ea_settings_sync_scheduler(service_pool: asyncpg.Pool) -> None:
    """Register the interval EA booking-settings mirror job."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning(
            "apscheduler not installed — EA settings sync scheduler disabled."
        )
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock

    interval_s = _interval_seconds()

    async def _job() -> None:
        await run_with_advisory_lock(
            service_pool,
            job_name="ea_settings_sync",
            lock_key=JOB_LOCK_KEYS["ea_settings_sync"],
            job=lambda: _sync_job(service_pool, interval_s=interval_s),
        )

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _job,
        IntervalTrigger(seconds=interval_s),
        id="ea_settings_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=interval_s,
    )
    _scheduler.start()
    logger.info(
        "ea_settings_sync: scheduler registered (every %ds)", interval_s
    )


def shutdown_ea_settings_sync_scheduler() -> None:
    """Gracefully stop the EA settings sync scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
