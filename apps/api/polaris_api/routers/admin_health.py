"""System Health snapshot endpoint — Phase 9 PR-6.

Single endpoint: GET /health (registered at /api/admin/health).

Aggregates 5 platform signals in parallel using asyncio.gather with
independent error handling per signal — one failing probe NEVER fails
the whole response. A simple TTL cache (60 s) throttles DB/Redis calls
so the 5-minute admin auto-refresh doesn't hammer infrastructure.

Signals:
  1. Supabase — SELECT 1 latency probe against the service pool.
  2. Redis    — PING + INFO memory.
  3. Broadcast queue depth — pending/in_flight/failed from broadcasts
     table + recipients.
  4. Ingestion last-run — per-tenant last ingestion_runs row summary.
  5. Meta rate-limit headroom — last 24 h X-Business-Use-Case-Usage
     header captures stored in meta_api_usage.

Auth: require_platform_admin (same as all /api/admin/* routes).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, Depends
from polaris_shared_types.platform_health import (
    ActiveTenants,
    ApmErrors,
    BroadcastQueueDepth,
    EmailDraftQueue,
    HealthSnapshot,
    KnowledgeGapCaptures,
    LlmTokenBurn,
    MessagingVolume,
    MetaRateLimitHeadroom,
    QuotaSaturation,
    QuotaSaturationTenant,
    RecentFailures,
    RedisStatus,
    ServiceConnectivity,
    SupabaseStatus,
    WebhookEventBreakdown,
    WebhookEventCount,
    WebhookEventError,
    WebhookSurfaceHealth,
)

from polaris_api.config import Settings, get_settings
from polaris_api.redis_pool import get_redis
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin import get_admin_service_conn

# Approximate list prices in MYR per 1M tokens (USD list price × ~4.7 FX,
# rounded). ESTIMATE ONLY for the at-a-glance burn tile — the Cost dashboard
# (Grafana Mimir) remains the source of record for billed spend.
_TOKEN_PRICE_MYR_PER_M: dict[str, dict[str, float]] = {
    "haiku": {"in": 3.8, "out": 18.8},
    "sonnet": {"in": 14.1, "out": 70.5},
    "gpt-4o-mini-fallback": {"in": 0.7, "out": 2.8},
}
_DEFAULT_TOKEN_PRICE_MYR_PER_M = {"in": 14.1, "out": 70.5}
# Top-N most-saturated tenants surfaced in the quota tile.
_QUOTA_TOP_N = 5

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-health"])

# ---------------------------------------------------------------------------
# Simple TTL cache — avoids hammering DB/Redis on rapid manual refreshes.
# A single dict is safe because FastAPI is single-process (no fork); async
# tasks see the same object.  No dep on cachetools or aiocache — per the
# "keep it simple, no new deps" constraint in the PR spec.
# ---------------------------------------------------------------------------
_CACHE_TTL_S = 60.0
_cache: dict[str, Any] = {}
_cache_expires_at: float = 0.0


def _cache_get() -> HealthSnapshot | None:
    if time.monotonic() < _cache_expires_at:
        payload: HealthSnapshot | None = _cache.get("snapshot")
        if payload is not None:
            return payload
    return None


def _cache_set(snapshot: HealthSnapshot) -> None:
    global _cache_expires_at  # noqa: PLW0603
    _cache["snapshot"] = snapshot
    _cache_expires_at = time.monotonic() + _CACHE_TTL_S


# ---------------------------------------------------------------------------
# Per-signal probes — each returns its typed model and MUST NOT raise.
# ---------------------------------------------------------------------------


async def _probe_supabase(conn: asyncpg.Connection) -> SupabaseStatus:
    """SELECT 1 latency probe against the service pool."""
    now = datetime.now(UTC).isoformat()
    t0 = time.monotonic()
    await conn.fetchval("SELECT 1")
    latency_ms = int((time.monotonic() - t0) * 1000)
    return SupabaseStatus(
        healthy=True,
        last_checked_at=now,
        db_latency_ms=latency_ms,
        error=None,
    )


async def _probe_redis() -> RedisStatus:
    """PING + INFO memory against the Redis singleton."""
    now = datetime.now(UTC).isoformat()
    redis = get_redis()
    await redis.ping()
    info: dict[str, Any] = await redis.info("memory")
    used_bytes: int = int(info.get("used_memory", 0))
    used_mb = max(0, used_bytes // (1024 * 1024))
    return RedisStatus(
        healthy=True,
        last_checked_at=now,
        used_memory_mb=used_mb,
        error=None,
    )


async def _probe_broadcasts(conn: asyncpg.Connection) -> BroadcastQueueDepth:
    """Broadcast queue counters from the broadcasts + broadcast_recipients tables."""
    # broadcast_status enum = draft|scheduled|running|paused|completed|cancelled|errored.
    # "pending" (queued, not yet started) maps to 'scheduled'; "in_flight" to 'running'.
    # Both broadcasts-table counters come from one COUNT(*) FILTER round-trip; the
    # failed-recipients counter is a separate query (different table).
    bcast = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'scheduled') AS pending,
            COUNT(*) FILTER (WHERE status = 'running')   AS in_flight
        FROM broadcasts
        """
    )
    failed_last_hour = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM broadcast_recipients
        WHERE status = 'failed'
          AND updated_at >= NOW() - INTERVAL '1 hour'
        """
    ) or 0
    return BroadcastQueueDepth(
        pending=int(bcast["pending"]) if bcast else 0,
        in_flight=int(bcast["in_flight"]) if bcast else 0,
        failed_last_hour=int(failed_last_hour),
    )


async def _probe_messaging(conn: asyncpg.Connection) -> MessagingVolume:
    """Inbound/outbound message volume (messages.role) + metered bot replies."""
    inbound = await conn.fetchval(
        "SELECT COUNT(*) FROM messages "
        "WHERE role = 'user' AND created_at >= NOW() - INTERVAL '24 hours'"
    ) or 0
    outbound = await conn.fetchval(
        "SELECT COUNT(*) FROM messages "
        "WHERE role = 'assistant' AND created_at >= NOW() - INTERVAL '24 hours'"
    ) or 0
    bot_replies_mtd = await conn.fetchval(
        "SELECT COALESCE(SUM(bot_replies), 0) FROM tenant_usage "
        "WHERE period_yyyymm = to_char(now(), 'YYYYMM')"
    ) or 0
    last_at = await conn.fetchval("SELECT MAX(created_at) FROM messages")
    return MessagingVolume(
        inbound_24h=int(inbound),
        outbound_24h=int(outbound),
        bot_replies_mtd=int(bot_replies_mtd),
        last_message_at=last_at.isoformat() if last_at else None,
    )


async def _probe_llm(conn: asyncpg.Connection) -> LlmTokenBurn:
    """Token throughput from messages; est MYR from a local price map (estimate)."""
    day = await conn.fetchrow(
        "SELECT COALESCE(SUM(input_tokens), 0) AS inp, "
        "COALESCE(SUM(output_tokens), 0) AS outp FROM messages "
        "WHERE created_at >= NOW() - INTERVAL '24 hours'"
    )
    month_rows = await conn.fetch(
        "SELECT model_used, COALESCE(SUM(input_tokens), 0) AS inp, "
        "COALESCE(SUM(output_tokens), 0) AS outp FROM messages "
        "WHERE created_at >= date_trunc('month', now()) GROUP BY model_used"
    )
    input_mtd = 0
    output_mtd = 0
    est_cost = 0.0
    for row in month_rows:
        inp = int(row["inp"])
        outp = int(row["outp"])
        input_mtd += inp
        output_mtd += outp
        price = _TOKEN_PRICE_MYR_PER_M.get(row["model_used"], _DEFAULT_TOKEN_PRICE_MYR_PER_M)
        est_cost += (inp / 1_000_000) * price["in"] + (outp / 1_000_000) * price["out"]
    return LlmTokenBurn(
        input_tokens_24h=int(day["inp"]) if day else 0,
        output_tokens_24h=int(day["outp"]) if day else 0,
        input_tokens_mtd=input_mtd,
        output_tokens_mtd=output_mtd,
        est_cost_myr_mtd=round(est_cost, 2),
    )


async def _probe_quota(conn: asyncpg.Connection) -> QuotaSaturation:
    """Bot-reply quota saturation: tenant_usage (consumed) vs tenant_quotas (cap)."""
    rows = await conn.fetch(
        """
        SELECT
            t.id::text         AS tenant_id,
            t.display_name     AS tenant_name,
            q.plan_tier::text  AS plan_tier,
            COALESCE(u.bot_replies, 0) AS consumed,
            q.bot_replies              AS cap
        FROM tenant_quotas q
        JOIN tenants t ON t.id = q.tenant_id
        LEFT JOIN tenant_usage u
          ON u.tenant_id = q.tenant_id
         AND u.period_yyyymm = to_char(now(), 'YYYYMM')
        WHERE t.status NOT IN ('deleted')
        """
    )
    tenants: list[QuotaSaturationTenant] = []
    near = 0
    over = 0
    for row in rows:
        consumed = int(row["consumed"])
        cap = int(row["cap"]) if row["cap"] is not None else None
        pct: float | None = None
        if cap and cap > 0:
            pct = round(consumed / cap * 100, 1)
            if pct >= 100:
                over += 1
            elif pct >= 80:
                near += 1
        tenants.append(
            QuotaSaturationTenant(
                tenant_id=row["tenant_id"],
                tenant_name=row["tenant_name"],
                plan_tier=row["plan_tier"],
                bot_replies=consumed,
                cap=cap,
                usage_pct=pct,
            )
        )
    tenants.sort(key=lambda t: (t.usage_pct if t.usage_pct is not None else -1.0), reverse=True)
    return QuotaSaturation(near_cap_count=near, over_cap_count=over, top=tenants[:_QUOTA_TOP_N])


async def _probe_failures(conn: asyncpg.Connection, settings: Settings) -> RecentFailures:
    """Recent failure counters + APM deep-links (links only when configured)."""
    errored_broadcasts = await conn.fetchval(
        "SELECT COUNT(*) FROM broadcasts "
        "WHERE status = 'errored' AND updated_at >= NOW() - INTERVAL '1 hour'"
    ) or 0
    failed_recipients = await conn.fetchval(
        "SELECT COUNT(*) FROM broadcast_recipients "
        "WHERE status = 'failed' AND updated_at >= NOW() - INTERVAL '1 hour'"
    ) or 0
    return RecentFailures(
        errored_broadcasts_last_hour=int(errored_broadcasts),
        failed_recipients_last_hour=int(failed_recipients),
        grafana_url=settings.grafana_dashboard_url or None,
        sentry_url=settings.sentry_project_url or None,
    )


async def _probe_meta_rate_limits(conn: asyncpg.Connection) -> list[MetaRateLimitHeadroom]:
    """Last 24 h Meta API usage header captures averaged per surface."""
    rows = await conn.fetch(
        """
        SELECT
            surface,
            AVG(calls_remaining_pct)::float  AS calls_remaining_pct,
            MAX(observed_at)::text           AS last_observed_at
        FROM meta_api_usage
        WHERE observed_at >= NOW() - INTERVAL '24 hours'
        GROUP BY surface
        ORDER BY surface
        """
    )
    result: list[MetaRateLimitHeadroom] = []
    for row in rows:
        result.append(
            MetaRateLimitHeadroom(
                surface=row["surface"],
                calls_remaining_pct=float(row["calls_remaining_pct"]),
                last_observed_at=str(row["last_observed_at"]),
            )
        )
    return result


async def _probe_email_drafts(conn: asyncpg.Connection) -> EmailDraftQueue:
    """Email assist draft queue counters from the email_drafts table.

    Single round-trip via COUNT(*) FILTER. ``pending`` is the all-time
    backlog (awaiting an outcome); accepted/edited/ignored are windowed to
    the last 24 h by ``resolved_at``. ``latest_draft_at`` is the all-time
    newest draft — an intentional staleness signal that stays meaningful
    even when there is no recent activity.
    """
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE outcome = 'pending') AS pending,
            COUNT(*) FILTER (
                WHERE outcome = 'accepted'
                  AND resolved_at >= NOW() - INTERVAL '24 hours'
            ) AS accepted_24h,
            COUNT(*) FILTER (
                WHERE outcome = 'edited'
                  AND resolved_at >= NOW() - INTERVAL '24 hours'
            ) AS edited_24h,
            COUNT(*) FILTER (
                WHERE outcome = 'ignored'
                  AND resolved_at >= NOW() - INTERVAL '24 hours'
            ) AS ignored_24h,
            MAX(posted_at) AS latest_at
        FROM email_drafts
        """
    )
    latest_at = row["latest_at"] if row else None
    return EmailDraftQueue(
        pending=int(row["pending"]) if row else 0,
        accepted_24h=int(row["accepted_24h"]) if row else 0,
        edited_24h=int(row["edited_24h"]) if row else 0,
        ignored_24h=int(row["ignored_24h"]) if row else 0,
        latest_draft_at=latest_at.isoformat() if latest_at else None,
    )


async def _probe_knowledge_gaps(conn: asyncpg.Connection) -> KnowledgeGapCaptures:
    """Knowledge gap miss captures from knowledge_gap_misses, last 24 h.

    Single round-trip via COUNT(*) FILTER per signal_type. ``latest_at`` is
    the all-time newest capture (staleness signal), intentionally not
    windowed to 24 h like the counters.
    """
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE signal_type = 'no_retrieval'
                  AND created_at >= NOW() - INTERVAL '24 hours'
            ) AS no_retrieval,
            COUNT(*) FILTER (
                WHERE signal_type = 'low_retrieval'
                  AND created_at >= NOW() - INTERVAL '24 hours'
            ) AS low_retrieval,
            COUNT(*) FILTER (
                WHERE signal_type = 'handoff'
                  AND created_at >= NOW() - INTERVAL '24 hours'
            ) AS handoff,
            MAX(created_at) AS latest_at
        FROM knowledge_gap_misses
        """
    )
    no_retrieval = int(row["no_retrieval"]) if row else 0
    low_retrieval = int(row["low_retrieval"]) if row else 0
    handoff = int(row["handoff"]) if row else 0
    latest_at = row["latest_at"] if row else None
    return KnowledgeGapCaptures(
        no_retrieval_24h=no_retrieval,
        low_retrieval_24h=low_retrieval,
        handoff_24h=handoff,
        total_24h=no_retrieval + low_retrieval + handoff,
        latest_at=latest_at.isoformat() if latest_at else None,
    )


async def _probe_active_tenants(conn: asyncpg.Connection) -> ActiveTenants:
    """Active tenant count (last 24h messages) + total provisioned.

    Both numerator and denominator exclude soft-deleted tenants so the
    "N of M active" ratio is consistent — without the subquery filter a
    deleted tenant's lingering messages could push active_24h above
    total_provisioned.
    """
    # Exclude demo tenants (ADR-066): is_demo covers brand demos + the
    # polaris-internal gateway, neither of which is a real provisioned customer.
    active_24h = await conn.fetchval(
        "SELECT COUNT(DISTINCT tenant_id) FROM messages "
        "WHERE created_at >= NOW() - INTERVAL '24 hours' "
        "AND tenant_id IN (SELECT id FROM tenants "
        "WHERE status NOT IN ('deleted') AND NOT is_demo)"
    ) or 0
    total_provisioned = await conn.fetchval(
        "SELECT COUNT(*) FROM tenants WHERE status NOT IN ('deleted') AND NOT is_demo"
    ) or 0
    return ActiveTenants(
        active_24h=int(active_24h),
        total_provisioned=int(total_provisioned),
    )


# Meta inbound webhooks all land under source='meta'; the lifecycle/status
# surfaces get their own source values (see migration 046). Aggregate them
# all for a single "Meta webhook delivery health" signal — per-surface
# WA/IG/Messenger headroom lives in the Meta Rate Limits tile.
_META_WEBHOOK_SOURCES = ("meta", "wa-status", "wa-account", "meta-template-status")


async def _probe_webhooks(
    conn: asyncpg.Connection, sources: Sequence[str]
) -> WebhookSurfaceHealth:
    """Webhook surface counters from webhook_events for the given sources.

    Two queries, both served by the (source, received_at) index from
    migration 102: a bounded 1 h window for the counters (index range
    scan) and an all-time MAX(received_at) as the staleness signal
    (index lookup, NOT windowed — "no webhooks in 6 h" is the alarm).
    The source list is passed as a parameter so the same probe serves
    both the Telegram tile (single source) and the aggregated Meta tile.
    """
    counts = await conn.fetchrow(
        """
        SELECT
            COUNT(*) AS events_1h,
            COUNT(*) FILTER (WHERE status = 'hmac_fail') AS hmac_fail_1h,
            COUNT(*) FILTER (WHERE status = 'tenant_not_found') AS tenant_not_found_1h
        FROM webhook_events
        WHERE source = ANY($1::text[])
          AND received_at >= NOW() - INTERVAL '1 hour'
        """,
        list(sources),
    )
    last_at = await conn.fetchval(
        "SELECT MAX(received_at) FROM webhook_events WHERE source = ANY($1::text[])",
        list(sources),
    )
    return WebhookSurfaceHealth(
        last_received_at=last_at.isoformat() if last_at else None,
        events_1h=int(counts["events_1h"]) if counts else 0,
        hmac_fail_1h=int(counts["hmac_fail_1h"]) if counts else 0,
        tenant_not_found_1h=int(counts["tenant_not_found_1h"]) if counts else 0,
    )


# webhook_events statuses that are NORMAL operational outcomes, not problems.
# Excluded from the error breakdown so high-volume benign events (replays,
# human-handled inbounds, demo routing) don't drown the signal. Everything else
# (hmac_fail, tenant_not_found, suspended_*, no_recipient, …) is surfaced.
_WEBHOOK_BENIGN_STATUSES = (
    "received",
    "processed",
    "replay_ignored",
    "human_handling",
    "demo_routed",
    "non_message_event",
    "chatwoot_deferred",
)
_WEBHOOK_BREAKDOWN_TOP_N = 8
_WEBHOOK_RECENT_LIMIT = 5  # matches the tile's display cap (no client-side slice)


def _top_webhook_counts(counts: dict[str, int]) -> list[WebhookEventCount]:
    """Top-N (key, count) buckets, ordered by count DESC then key ASC.

    The secondary sort on the key makes the order DETERMINISTIC when counts tie:
    Postgres GROUP BY returns rows in an unspecified order, so without it two
    equal-count buckets could swap places between refreshes.
    """
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [WebhookEventCount(key=k, count=v) for k, v in ranked[:_WEBHOOK_BREAKDOWN_TOP_N]]


async def _probe_webhook_event_errors(
    conn: asyncpg.Connection, settings: Settings
) -> WebhookEventBreakdown:
    """Webhook ingest error breakdown from ``webhook_events`` (last 24 h).

    "Error" = any status NOT in ``_WEBHOOK_BENIGN_STATUSES``. One grouped query
    for the status/error_class counts + one bounded query for the recent rows,
    both windowed to 24 h. (The ``(status, received_at)`` index from migration
    015 covers the window scan; the ``status <> ALL`` exclusion is a negative
    predicate, so this is an index/heap scan of the bounded window, not a seek —
    fine at this table size and refresh cadence.) NULL ``error_class`` →
    '(none)'; lists are capped top-N via ``_top_webhook_counts``.
    """
    grouped = await conn.fetch(
        """
        SELECT status, error_class, COUNT(*) AS c
        FROM webhook_events
        WHERE received_at >= NOW() - INTERVAL '24 hours'
          AND status <> ALL($1::text[])
        GROUP BY status, error_class
        """,
        list(_WEBHOOK_BENIGN_STATUSES),
    )
    by_status: dict[str, int] = {}
    by_error_class: dict[str, int] = {}
    total = 0
    for row in grouped:
        c = int(row["c"])
        total += c
        status = str(row["status"])
        by_status[status] = by_status.get(status, 0) + c
        ec = row["error_class"] or "(none)"
        by_error_class[ec] = by_error_class.get(ec, 0) + c

    recent_rows = await conn.fetch(
        """
        SELECT received_at, source, status, error_class
        FROM webhook_events
        WHERE received_at >= NOW() - INTERVAL '24 hours'
          AND status <> ALL($1::text[])
        ORDER BY received_at DESC
        LIMIT $2
        """,
        list(_WEBHOOK_BENIGN_STATUSES),
        _WEBHOOK_RECENT_LIMIT,
    )
    recent = [
        WebhookEventError(
            # .astimezone(UTC) makes the RFC3339 'Z'/'+00:00' suffix unconditional
            # regardless of how the column/driver yields tzinfo.
            received_at=row["received_at"].astimezone(UTC).isoformat(),
            source=str(row["source"]),
            status=str(row["status"]),
            error_class=row["error_class"],
        )
        for row in recent_rows
    ]
    return WebhookEventBreakdown(
        total_errors_24h=total,
        by_status=_top_webhook_counts(by_status),
        by_error_class=_top_webhook_counts(by_error_class),
        recent=recent,
        grafana_url=settings.grafana_dashboard_url or None,
        error=None,
    )


# ---------------------------------------------------------------------------
# HTTP probes — do NOT use the asyncpg connection; run concurrently.
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = 1.5


async def _probe_chatwoot(settings: Settings) -> ServiceConnectivity:
    """HTTP reachability probe for Chatwoot — any response means reachable.

    No try/except here: connection/timeout errors propagate to
    _safe_chatwoot, which renders the degraded (healthy=False) tile.
    A non-2xx or 3xx response still proves the host is up, so we don't
    call raise_for_status or follow redirects.
    """
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        await client.get(settings.chatwoot_base_url)
    latency_ms = int((time.monotonic() - t0) * 1000)
    return ServiceConnectivity(
        healthy=True,
        last_checked_at=datetime.now(UTC).isoformat(),
        latency_ms=latency_ms,
        error=None,
    )


async def _probe_ea(settings: Settings) -> ServiceConnectivity:
    """Customer-facing reachability probe for the EA booking host (QA finding H1).

    Hits the PUBLIC ``ea_health_url`` (through Cloudflare → Caddy → nginx → php)
    rather than the internal ``ea_api_base_url`` — the internal nginx answers
    200 on ``/`` even when the booking origin is 502-ing, which made this tile
    falsely green during a real customer-facing outage.

    httpx does NOT raise on a 5xx response, so we MUST inspect the status: a
    gateway/origin error (>= 500) means customers can't book → degraded tile.
    Transport errors (DNS / connect / timeout) still propagate to _safe_ea.
    """
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(settings.ea_health_url)
    latency_ms = int((time.monotonic() - t0) * 1000)
    healthy = resp.status_code < 500
    return ServiceConnectivity(
        healthy=healthy,
        last_checked_at=datetime.now(UTC).isoformat(),
        latency_ms=latency_ms,
        error=None if healthy else f"HTTP {resp.status_code} from booking host",
    )


async def _probe_apm(settings: Settings) -> ApmErrors:
    """Sentry error count for the last 24 h via the Sentry stats API."""
    sentry_url = settings.sentry_project_url or None
    if not settings.sentry_auth_token:
        return ApmErrors(configured=False, error_count_24h=None, sentry_url=sentry_url, error=None)
    try:
        url = (
            f"{settings.sentry_api_base}/api/0/projects/"
            f"{settings.sentry_org_slug}/{settings.sentry_project_slug}/stats/"
        )
        headers = {"Authorization": f"Bearer {settings.sentry_auth_token}"}
        # statsPeriod bounds the window server-side so we can sum every
        # returned bucket (no fragile [-24:] slice that assumes hourly
        # resolution). NOTE: stat='received' counts ingested events — for an
        # error-monitoring project that's effectively the error count; if the
        # project also ingests transactions, switch to the org stats_v2
        # endpoint with category=error for a strict errors-only figure.
        params = {"stat": "received", "resolution": "1h", "statsPeriod": "24h"}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        # Response is [[unix_ts, count], ...] over the statsPeriod window.
        data: list[list[Any]] = resp.json()
        error_count_24h = int(sum(entry[1] for entry in data))
        return ApmErrors(
            configured=True,
            error_count_24h=error_count_24h,
            sentry_url=sentry_url,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        return ApmErrors(
            configured=True,
            error_count_24h=None,
            sentry_url=sentry_url,
            error=str(exc)[:500],
        )


# ---------------------------------------------------------------------------
# Safe wrappers — catch any exception and return a degraded model.
# ---------------------------------------------------------------------------

async def _run_probe[T](
    coro: Awaitable[T],
    fallback: Callable[[Exception], T],
    label: str,
) -> T:
    """Await a probe; on any error log a warning and return ``fallback(exc)``.

    Centralises the try/except/log boilerplate the per-tile wrappers repeat.
    ``fallback`` takes the exception so wrappers that embed ``error=str(exc)``
    (or a settings-derived value) in their degraded model keep working.
    """
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s probe failed: %s", label, exc)
        return fallback(exc)


async def _safe_supabase(conn: asyncpg.Connection) -> SupabaseStatus:
    return await _run_probe(
        _probe_supabase(conn),
        lambda exc: SupabaseStatus(
            healthy=False,
            last_checked_at=datetime.now(UTC).isoformat(),
            db_latency_ms=None,
            error=str(exc)[:500],
        ),
        "supabase",
    )


async def _safe_redis() -> RedisStatus:
    return await _run_probe(
        _probe_redis(),
        lambda exc: RedisStatus(
            healthy=False,
            last_checked_at=datetime.now(UTC).isoformat(),
            used_memory_mb=None,
            error=str(exc)[:500],
        ),
        "redis",
    )


async def _safe_broadcasts(conn: asyncpg.Connection) -> BroadcastQueueDepth:
    return await _run_probe(
        _probe_broadcasts(conn),
        lambda _exc: BroadcastQueueDepth(pending=0, in_flight=0, failed_last_hour=0),
        "broadcasts",
    )


async def _safe_messaging(conn: asyncpg.Connection) -> MessagingVolume:
    return await _run_probe(
        _probe_messaging(conn),
        lambda _exc: MessagingVolume(
            inbound_24h=0, outbound_24h=0, bot_replies_mtd=0, last_message_at=None
        ),
        "messaging",
    )


async def _safe_llm(conn: asyncpg.Connection) -> LlmTokenBurn:
    return await _run_probe(
        _probe_llm(conn),
        lambda _exc: LlmTokenBurn(
            input_tokens_24h=0,
            output_tokens_24h=0,
            input_tokens_mtd=0,
            output_tokens_mtd=0,
            est_cost_myr_mtd=None,
        ),
        "llm",
    )


async def _safe_quota(conn: asyncpg.Connection) -> QuotaSaturation:
    return await _run_probe(
        _probe_quota(conn),
        lambda _exc: QuotaSaturation(near_cap_count=0, over_cap_count=0, top=[]),
        "quota",
    )


async def _safe_failures(conn: asyncpg.Connection, settings: Settings) -> RecentFailures:
    return await _run_probe(
        _probe_failures(conn, settings),
        lambda _exc: RecentFailures(
            errored_broadcasts_last_hour=0,
            failed_recipients_last_hour=0,
            grafana_url=settings.grafana_dashboard_url or None,
            sentry_url=settings.sentry_project_url or None,
        ),
        "failures",
    )


async def _safe_meta_rate_limits(conn: asyncpg.Connection) -> list[MetaRateLimitHeadroom]:
    return await _run_probe(
        _probe_meta_rate_limits(conn),
        lambda _exc: [],
        "meta_rate_limits",
    )


async def _safe_email_drafts(conn: asyncpg.Connection) -> EmailDraftQueue:
    return await _run_probe(
        _probe_email_drafts(conn),
        lambda _exc: EmailDraftQueue(
            pending=0, accepted_24h=0, edited_24h=0, ignored_24h=0, latest_draft_at=None
        ),
        "email_drafts",
    )


async def _safe_knowledge_gaps(conn: asyncpg.Connection) -> KnowledgeGapCaptures:
    return await _run_probe(
        _probe_knowledge_gaps(conn),
        lambda _exc: KnowledgeGapCaptures(
            no_retrieval_24h=0,
            low_retrieval_24h=0,
            handoff_24h=0,
            total_24h=0,
            latest_at=None,
        ),
        "knowledge_gaps",
    )


async def _safe_active_tenants(conn: asyncpg.Connection) -> ActiveTenants:
    return await _run_probe(
        _probe_active_tenants(conn),
        lambda _exc: ActiveTenants(active_24h=0, total_provisioned=0),
        "active_tenants",
    )


async def _safe_telegram_webhooks(conn: asyncpg.Connection) -> WebhookSurfaceHealth:
    return await _run_probe(
        _probe_webhooks(conn, ("telegram",)),
        lambda _exc: WebhookSurfaceHealth(
            last_received_at=None, events_1h=0, hmac_fail_1h=0, tenant_not_found_1h=0
        ),
        "telegram_webhooks",
    )


async def _safe_meta_webhooks(conn: asyncpg.Connection) -> WebhookSurfaceHealth:
    return await _run_probe(
        _probe_webhooks(conn, _META_WEBHOOK_SOURCES),
        lambda _exc: WebhookSurfaceHealth(
            last_received_at=None, events_1h=0, hmac_fail_1h=0, tenant_not_found_1h=0
        ),
        "meta_webhooks",
    )


async def _safe_webhook_event_errors(
    conn: asyncpg.Connection, settings: Settings
) -> WebhookEventBreakdown:
    return await _run_probe(
        _probe_webhook_event_errors(conn, settings),
        # error set so the tile renders a distinct "probe unavailable" state —
        # all-zero counts would otherwise read as a healthy "no errors".
        lambda exc: WebhookEventBreakdown(
            total_errors_24h=0,
            by_status=[],
            by_error_class=[],
            recent=[],
            grafana_url=settings.grafana_dashboard_url or None,
            error=str(exc)[:500],
        ),
        "webhook_event_errors",
    )


async def _safe_chatwoot(settings: Settings) -> ServiceConnectivity:
    return await _run_probe(
        _probe_chatwoot(settings),
        lambda exc: ServiceConnectivity(
            healthy=False,
            last_checked_at=datetime.now(UTC).isoformat(),
            latency_ms=None,
            error=str(exc)[:500],
        ),
        "chatwoot",
    )


async def _safe_ea(settings: Settings) -> ServiceConnectivity:
    return await _run_probe(
        _probe_ea(settings),
        lambda exc: ServiceConnectivity(
            healthy=False,
            last_checked_at=datetime.now(UTC).isoformat(),
            latency_ms=None,
            error=str(exc)[:500],
        ),
        "ea",
    )


async def _safe_apm(settings: Settings) -> ApmErrors:
    return await _run_probe(
        _probe_apm(settings),
        lambda exc: ApmErrors(
            configured=bool(settings.sentry_auth_token),
            error_count_24h=None,
            sentry_url=settings.sentry_project_url or None,
            error=str(exc)[:500],
        ),
        "apm",
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    response_model=HealthSnapshot,
    summary="System health snapshot — platform signals aggregated in parallel",
)
async def get_health_snapshot(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> HealthSnapshot:
    """Return a HealthSnapshot aggregating all platform signals.

    Results are cached for 60 s. Each signal is probed independently —
    a failure on one tile never crashes the others. DB probes run
    sequentially on the shared connection (asyncpg can't multiplex one
    connection); Redis runs concurrently. Queries are tiny — total ≤ 2 s.
    """
    cached = _cache_get()
    if cached is not None:
        return cached

    settings = get_settings()
    # All DB probes share ONE asyncpg connection (the injected dependency), and
    # asyncpg cannot multiplex concurrent operations on a single connection
    # ("another operation is in progress"). So the DB probes run SEQUENTIALLY;
    # only the Redis and HTTP probes (no DB connection) run concurrently alongside
    # them. Each query is tiny — total latency stays well under the 2 s budget.
    redis_task = asyncio.ensure_future(_safe_redis())
    chatwoot_task = asyncio.ensure_future(_safe_chatwoot(settings))
    ea_task = asyncio.ensure_future(_safe_ea(settings))
    apm_task = asyncio.ensure_future(_safe_apm(settings))
    supabase = await _safe_supabase(conn)
    broadcasts = await _safe_broadcasts(conn)
    messaging = await _safe_messaging(conn)
    llm = await _safe_llm(conn)
    quota = await _safe_quota(conn)
    failures = await _safe_failures(conn, settings)
    meta_rate_limits = await _safe_meta_rate_limits(conn)
    email_drafts = await _safe_email_drafts(conn)
    knowledge_gaps = await _safe_knowledge_gaps(conn)
    active_tenants = await _safe_active_tenants(conn)
    telegram_webhooks = await _safe_telegram_webhooks(conn)
    meta_webhooks = await _safe_meta_webhooks(conn)
    webhook_event_errors = await _safe_webhook_event_errors(conn, settings)
    redis = await redis_task
    chatwoot = await chatwoot_task
    ea = await ea_task
    apm = await apm_task

    snapshot = HealthSnapshot(
        supabase=supabase,
        redis=redis,
        broadcasts=broadcasts,
        messaging=messaging,
        llm=llm,
        quota=quota,
        failures=failures,
        meta_rate_limits=meta_rate_limits,
        email_drafts=email_drafts,
        knowledge_gaps=knowledge_gaps,
        active_tenants=active_tenants,
        chatwoot=chatwoot,
        ea=ea,
        telegram_webhooks=telegram_webhooks,
        meta_webhooks=meta_webhooks,
        webhook_event_errors=webhook_event_errors,
        apm=apm,
        refreshed_at=datetime.now(UTC).isoformat(),
    )
    _cache_set(snapshot)
    return snapshot
