"""Nightly LLM cost ceiling checker — Phase 10 PR-4.

APScheduler cron: 03:00 MYT (UTC+8).  Runs after retention_sweep (02:00)
and before audit_chain_verify (04:00).

Two passes per run:

1. **Per-tenant ceiling check** (active tenants with non-null ceiling):
   Reads ``tenant_quotas.llm_cost_ceiling_myr``; fetches current-month
   cost from Mimir; fires warning email at 75% and Sentry fatal +
   auto-disable Cohere at 100%.

2. **Platform spend check**:
   Sums current-month cost across all active tenants; fires Sentry
   fatal if total exceeds ``POLARIS_PLATFORM_DAILY_SPEND_THRESHOLD_MYR``.

Per-tenant error isolation — one bad tenant never aborts the whole run.
Both checks degrade gracefully when Mimir is not configured.

ADR refs: ADR-035 (Sentry alerting), ADR-026 (plan tier caps).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from polaris_api.services.cost_ceiling import (
    apply_tenant_ceiling_breach,
    check_platform_daily_spend,
    fetch_tenant_ceiling_status,
)

logger = logging.getLogger(__name__)

_scheduler = None


# ── main job ──────────────────────────────────────────────────────────────────


async def _cost_ceiling_job(service_pool: asyncpg.Pool) -> None:
    """One full cost ceiling check pass."""
    from polaris_api.config import get_settings

    run_start = datetime.now(UTC)
    logger.info("cost_ceiling_check: run started at %s", run_start.isoformat())

    settings = get_settings()
    mimir_url: str = getattr(settings, "grafana_cloud_mimir_url", "") or ""
    mimir_token: str = getattr(settings, "grafana_cloud_mimir_token", "") or ""
    threshold_myr: float = getattr(
        settings, "platform_daily_spend_threshold_myr", 0.0
    ) or 0.0

    if not mimir_url or not mimir_token:
        logger.info(
            "cost_ceiling_check: Mimir not configured — skipping "
            "(set GRAFANA_CLOUD_MIMIR_URL + GRAFANA_CLOUD_MIMIR_TOKEN)"
        )
        return

    yyyymm = run_start.strftime("%Y%m")
    checked = errors = 0

    # audit Batch B (B7-c): snapshot the ceiling-configured tenant list on a
    # short-lived conn, then RELEASE it before the per-tenant Mimir HTTP
    # loop — previously one service-pool conn was held across every
    # tenant's (slow) Mimir fetch for the whole job.
    async with service_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.id AS tenant_id,
                   t.display_name,
                   tq.llm_cost_ceiling_myr
              FROM tenants t
              JOIN tenant_quotas tq ON tq.tenant_id = t.id
             WHERE t.status = 'active'
               AND tq.llm_cost_ceiling_myr IS NOT NULL
               AND tq.llm_cost_ceiling_myr > 0
             ORDER BY t.created_at
            """
        )

    for row in rows:
        tenant_id: UUID = row["tenant_id"]
        try:
            # Phase 1 (HTTP, no conn): fetch + classify this tenant's cost.
            check = await fetch_tenant_ceiling_status(
                tenant_id=tenant_id,
                tenant_name=row["display_name"] or str(tenant_id),
                ceiling_myr=Decimal(str(row["llm_cost_ceiling_myr"])),
                yyyymm=yyyymm,
                mimir_url=mimir_url,
                mimir_token=mimir_token,
            )
            # Phase 2 (conn, no HTTP): act on a breach, if any. A fresh
            # per-tenant conn — only acquired when there's actually a DB
            # write to make (hard breach) or audit row; healthy/warn-only
            # tenants still route through here since email-only alerts
            # don't need a conn, but acquiring uniformly keeps this loop
            # simple and the conn is released immediately after.
            async with service_pool.acquire() as conn:
                await apply_tenant_ceiling_breach(conn, check, settings=settings)
            checked += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cost_ceiling_check: error tenant=%s: %s — skipping",
                tenant_id, exc,
            )
            errors += 1

    # Platform-wide spend check: acquires its own short-lived conn for the
    # tenant-list read, then loops Mimir HTTP conn-free (services/cost_ceiling.py).
    try:
        await check_platform_daily_spend(
            service_pool,
            threshold_myr=threshold_myr,
            mimir_url=mimir_url,
            mimir_token=mimir_token,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "cost_ceiling_check: platform spend check failed: %s", exc,
        )
        errors += 1

    elapsed = (datetime.now(UTC) - run_start).total_seconds()
    logger.info(
        "cost_ceiling_check: done checked=%d errors=%d elapsed_s=%.1f",
        checked, errors, elapsed,
    )


# ── scheduler registration ────────────────────────────────────────────────────


def register_cost_ceiling_scheduler(service_pool: asyncpg.Pool) -> None:
    """Register the nightly 03:00 MYT cost ceiling check job."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed — cost ceiling scheduler disabled.")
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock

    async def _job() -> None:
        await run_with_advisory_lock(
            service_pool,
            job_name="cost_ceiling_check",
            lock_key=JOB_LOCK_KEYS["cost_ceiling_check"],
            job=lambda: _cost_ceiling_job(service_pool),
        )

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _job,
        CronTrigger(hour=3, minute=0, timezone="Asia/Kuala_Lumpur"),
        id="cost_ceiling_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("cost_ceiling_check: scheduler registered (03:00 MYT daily)")


def shutdown_cost_ceiling_scheduler() -> None:
    """Gracefully stop the cost ceiling scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
