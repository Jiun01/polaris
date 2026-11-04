"""Nightly bot-replies counter flush — Phase 10 PR-1.

APScheduler job registered at 00:05 Asia/Kuala_Lumpur.

What it does (idempotent — safe to re-run if it fires twice):
  1. Determine the current period (YYYYMM).
  2. SCAN Redis for all ``bot_replies:*:{YYYYMM}`` keys.
  3. For each key: parse the tenant_id; UPSERT the count into ``tenant_usage``
     (append-only history row) and UPDATE ``tenant_quotas.bot_replies_used_
     this_period`` (the portal Usage-page column).
  4. Log the flush summary (tenant count + total replies seen).

The job uses the service pool (BYPASSRLS) because it writes across all
tenants in a single run.  It does NOT delete Redis keys after flushing —
the counter stays live so mid-day queries are still fast; the 35-day TTL
on each key is the natural expiry.

Error handling:
  - Orphan counters (a Redis ``bot_replies:*`` key whose tenant no longer
    exists in ``tenants``) are filtered out BEFORE the write — they would
    otherwise hit the ``tenant_usage_tenant_id_fkey`` foreign key and log a
    nightly ERROR forever (until the key's 35-day TTL expires).  This happens
    when a tenant is hard-deleted/wiped while a live counter lingers.  Skipped
    orphans are surfaced in one aggregated WARNING, not a per-tenant ERROR, so
    the Sentry alert rule (which keys on ERROR) doesn't trip on benign state.
  - A single-tenant write failure is logged and skipped; the loop
    continues for the remaining tenants.  A partial flush is better than
    a full abort — the next night's run will catch missed tenants.
  - If Redis is down, the job logs an error and returns immediately.  The
    nightly audit alert (Phase 10 PR-2 Sentry rule) will fire on the log
    error within the 5-minute Sentry window.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)
_scheduler = None


class FlushResult(NamedTuple):
    """Outcome of one :func:`_flush_counters` pass.

    ``flushed``       — counters successfully written to ``tenant_usage`` +
                        ``tenant_quotas``.
    ``errors``        — counters that raised on write (genuine failures; the
                        loop isolates them and continues).
    ``total_replies`` — sum of ``bot_replies`` across flushed counters.
    ``skipped``       — counters skipped because their tenant_id is not in
                        ``tenants`` (orphan) or is not a valid UUID.
    """

    flushed: int
    errors: int
    total_replies: int
    skipped: int


async def _known_tenant_ids(
    conn: asyncpg.Connection, candidates: list[UUID]
) -> set[UUID]:
    """Return the subset of ``candidates`` that exist in ``tenants``.

    One query, scoped to the candidate ids (not the whole table), so the
    orphan filter stays cheap as the tenant count grows.
    """
    if not candidates:
        return set()
    rows = await conn.fetch(
        "SELECT id FROM tenants WHERE id = ANY($1::uuid[])", candidates
    )
    return {row["id"] for row in rows}


async def _flush_counters(
    service_pool: asyncpg.Pool,
    counters: dict[str, int],
    *,
    period: str,
) -> FlushResult:
    """Write every active counter to ``tenant_usage`` + ``tenant_quotas``.

    Orphan counters — a tenant_id that is not a valid UUID, or that no longer
    exists in ``tenants`` — are filtered out before the write so they can't
    raise ``tenant_usage_tenant_id_fkey`` and spam a nightly ERROR.

    audit Batch B (B7-d): the surviving counters are written with ONE
    ``service_pool.acquire()`` + ONE transaction, via two bulk ``UNNEST``
    statements over arrays — replacing the previous per-tenant acquire+txn
    loop (N+1 acquires for N known tenants). ``tenant_usage`` /
    ``tenant_quotas`` carry NO audit hash chain (unlike ``audit_logs`` —
    see ``services/audit.py``), so a bulk write is safe here; per-tenant
    error isolation is intentionally traded for the single round-trip: on a
    batch failure the WHOLE flush for this period is logged as an error and
    retried next run (Redis counters are untouched either way — the
    existing crash-safety contract — so nothing is lost).
    """
    # Parse tenant ids up front; an unparseable key segment is an orphan too.
    parsed: dict[UUID, tuple[str, int]] = {}
    unparseable: list[str] = []
    for tenant_id_str, count in counters.items():
        try:
            parsed[UUID(tenant_id_str)] = (tenant_id_str, count)
        except (ValueError, AttributeError):
            unparseable.append(tenant_id_str)

    flushed = 0
    errors = 0
    total_replies = 0
    skipped_ids: list[str] = list(unparseable)

    async with service_pool.acquire() as conn:
        known = await _known_tenant_ids(conn, list(parsed))

        orphans = [s for tid, (s, _) in parsed.items() if tid not in known]
        skipped_ids.extend(orphans)
        if skipped_ids:
            logger.warning(
                "quota_flush: skipped %d orphan counter(s) period=%s "
                "(tenant_id absent from tenants or not a UUID): %s",
                len(skipped_ids),
                period,
                ", ".join(sorted(skipped_ids)),
            )

        if not known:
            return FlushResult(
                flushed=0, errors=0, total_replies=0, skipped=len(skipped_ids),
            )

        ids = [str(tid) for tid in known]
        counts = [parsed[tid][1] for tid in known]

        try:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO tenant_usage
                        (tenant_id, period_yyyymm, bot_replies, flushed_at)
                    SELECT tid, $2, cnt, now()
                      FROM unnest($1::uuid[], $3::int[]) AS u(tid, cnt)
                    ON CONFLICT (tenant_id, period_yyyymm)
                    DO UPDATE SET
                        bot_replies = EXCLUDED.bot_replies,
                        flushed_at  = EXCLUDED.flushed_at
                    """,
                    ids,
                    period,
                    counts,
                )
                await conn.execute(
                    """
                    UPDATE tenant_quotas tq
                       SET bot_replies_used_this_period = u.cnt,
                           updated_at = now()
                      FROM unnest($1::uuid[], $2::int[]) AS u(tid, cnt)
                     WHERE tq.tenant_id = u.tid
                    """,
                    ids,
                    counts,
                )
            flushed = len(known)
            total_replies = sum(counts)
        except Exception as exc:  # noqa: BLE001
            # Bulk write failed — per-tenant granularity is lost (accepted
            # tradeoff, see docstring). Counters stay in Redis (35-day TTL);
            # the next nightly run retries the whole period.
            errors = len(known)
            logger.error(
                "quota_flush: bulk flush failed period=%s tenants=%d err=%s",
                period, len(known), exc, exc_info=True,
            )

    return FlushResult(
        flushed=flushed,
        errors=errors,
        total_replies=total_replies,
        skipped=len(skipped_ids),
    )


def register_quota_flush_scheduler(service_pool: object, redis: object) -> None:
    """Register the 00:05 MYT nightly quota-flush job.

    Args:
        service_pool: asyncpg pool with BYPASSRLS (``get_service_pool()``).
        redis:        The shared redis.asyncio.Redis singleton.
    """
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed — quota flush scheduler disabled.")
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock
    from polaris_api.services.quota import get_all_active_counters, period_yyyymm

    async def _run() -> None:
        period = period_yyyymm(datetime.now(UTC))
        logger.info("quota_flush: starting flush for period=%s", period)

        try:
            counters = await get_all_active_counters(redis, period_yyyymm=period)  # type: ignore[arg-type]
        except Exception as exc:
            logger.error(
                "quota_flush: Redis SCAN failed period=%s err=%s",
                period, exc, exc_info=True,
            )
            return

        if not counters:
            logger.info("quota_flush: no active counters for period=%s", period)
            return

        result = await _flush_counters(
            service_pool,
            counters,
            period=period,
        )

        logger.info(
            "quota_flush: complete period=%s tenants_flushed=%d "
            "total_replies=%d errors=%d skipped=%d",
            period,
            result.flushed,
            result.total_replies,
            result.errors,
            result.skipped,
        )

    async def _flush_job() -> None:
        await run_with_advisory_lock(
            service_pool,
            job_name="quota_flush",
            lock_key=JOB_LOCK_KEYS["quota_flush"],
            job=_run,
        )

    _scheduler = AsyncIOScheduler(timezone="Asia/Kuala_Lumpur")
    _scheduler.add_job(
        _flush_job,
        CronTrigger(hour=0, minute=5, timezone="Asia/Kuala_Lumpur"),
        id="quota_flush_nightly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("quota_flush: scheduled at 00:05 MYT")


def shutdown_quota_flush_scheduler() -> None:
    """Stop the quota flush scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
