"""Bot-replies Redis counter + tier-cap enforcement — Phase 10 PR-1.

ADR-017: one counter per tenant per calendar month.
ADR-026: tier monthly caps — Starter 5k / Pro 25k / Ultra 60k / Enterprise custom.

Redis key schema:
    bot_replies:{tenant_id}:{YYYYMM}
    e.g.  bot_replies:550e8400-e29b-41d4-a716-446655440000:202605

Two public coroutines used by pipeline.py:

  ``check_quota(conn, redis, tenant_id)``
      Reads the period cap from ``tenant_quotas.bot_replies_period_cap``
      (already populated by the billing cycle on provisioning / plan-change)
      and the current Redis counter.  Returns a ``QuotaStatus`` namedtuple.
      One DB round-trip per inbound (SELECT on tenant_quotas, already in
      the hot-path transaction so the connection is warm).

  ``increment_counter(redis, tenant_id)``
      INCR the Redis key and refresh the TTL to 35 days.  Called AFTER a
      reply is successfully dispatched (pipeline step 12b).

One helper used by the quota_flush scheduled job:

  ``get_all_active_counters(redis, period_yyyymm)``
      SCAN for all keys matching the given period and return a mapping of
      {tenant_id_str: count}.  Used for the nightly flush to tenant_usage.

Soft-cap threshold (80%) is available as ``SOFT_CAP_PCT`` for callers
that want to emit a warning header before the hard cap is reached.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Final, NamedTuple
from uuid import UUID

import asyncpg
import redis.asyncio as redis_async

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

# ADR-026 tier defaults (used as fallback when tenant_quotas row is missing).
_TIER_CAP_DEFAULTS: dict[str, int] = {
    "starter": 5_000,
    "pro": 25_000,
    "ultra": 60_000,
    "enterprise": 60_000,  # custom; fallback to ultra; admin sets real cap
}

SOFT_CAP_PCT: float = 0.80
"""Fraction of cap at which the pipeline emits the soft-cap warn header."""

_KEY_TTL_SECONDS: int = 86_400 * 35  # 35 days — survives end-of-month + flush grace

# audit Batch B (B5): Redis cache for the `tenant_quotas.bot_replies_period_cap`
# read — mirrors rag.py's `tier:` cache (Pattern 2). A plan change now takes up
# to 60s to affect the enforced cap (accepted per audit — SMB traffic; no
# invalidation hooks built). Only REAL rows are cached — the missing-row
# starter-default fallback below is never cached (a just-provisioned tenant
# would otherwise be stuck on the default for 60s after its row lands).
_CAP_CACHE_PREFIX: Final[str] = "quota_cap:"
_CAP_CACHE_TTL_SECONDS: Final[int] = 60


# ── public shapes ─────────────────────────────────────────────────────────────


class QuotaStatus(NamedTuple):
    """Result of a ``check_quota()`` call.

    ``over_cap``:      True → hard cap exceeded; pipeline must abort.
    ``at_soft_cap``:   True → ≥ 80 % used; caller may emit warn header.
    ``current_count``: Replies sent this period (from Redis; may lag DB by
                       up to one nightly flush cycle — acceptable).
    ``period_cap``:    The active cap for this period.
    """

    over_cap: bool
    at_soft_cap: bool
    current_count: int
    period_cap: int


# ── key helpers ───────────────────────────────────────────────────────────────


def period_yyyymm(now: datetime | None = None) -> str:
    """Return the current period as 'YYYYMM' string."""
    dt = now if now is not None else datetime.now(UTC)
    return dt.strftime("%Y%m")


def _redis_key(tenant_id: UUID, period: str | None = None) -> str:
    """Build the Redis key for a tenant's counter in the given period."""
    p = period if period is not None else period_yyyymm()
    return f"bot_replies:{tenant_id}:{p}"


# ── pipeline-facing coroutines ────────────────────────────────────────────────


async def check_quota(
    conn: asyncpg.Connection,
    redis: redis_async.Redis,
    *,
    tenant_id: UUID,
    now: datetime | None = None,
) -> QuotaStatus:
    """Check whether the tenant has remaining quota this period.

    Reads ``tenant_quotas.bot_replies_period_cap`` at most once per 60s per
    tenant (audit Batch B / B5) via a Redis cache mirroring
    ``rag.py``'s ``tier:`` pattern — before Batch B this fetchrow ran on
    EVERY inbound message even though the cap rarely changes mid-period.
    The connection is already open in the pipeline transaction so a cache
    miss is a warm round-trip, not a new connection.

    Falls back to the tier-default cap if the quota row is missing
    (should not happen post-provisioning, but safe under test fixtures
    that don't seed quota rows). The missing-row fallback is NEVER cached.
    """
    cache_key = f"{_CAP_CACHE_PREFIX}{tenant_id}"
    period_cap: int | None = None

    cached = await redis.get(cache_key)
    if cached is not None:
        payload = cached.decode("utf-8") if isinstance(cached, bytes) else cached
        try:
            period_cap = int(json.loads(payload)["period_cap"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            period_cap = None  # corrupt cache entry — treat as a miss

    if period_cap is None:
        # Read cap from DB (period-scoped working value, already prorated for
        # mid-cycle signup / upgrade).
        cap_row = await conn.fetchrow(
            """
            SELECT bot_replies_period_cap, plan_tier
              FROM tenant_quotas
             WHERE tenant_id = $1
            """,
            tenant_id,
        )
        if cap_row is not None:
            period_cap = int(cap_row["bot_replies_period_cap"])
            await redis.set(
                cache_key,
                json.dumps({"period_cap": period_cap}),
                ex=_CAP_CACHE_TTL_SECONDS,
            )
        else:
            # Fallback: use starter default and log so we know to investigate.
            # NOT cached — a just-provisioned tenant must not be stuck on the
            # default for 60s after its tenant_quotas row lands.
            logger.warning(
                "quota.check_quota: tenant_quotas row missing for tenant=%s — "
                "using starter default cap %d",
                tenant_id,
                _TIER_CAP_DEFAULTS["starter"],
            )
            period_cap = _TIER_CAP_DEFAULTS["starter"]

    # Read current count from Redis.
    key = _redis_key(tenant_id, period_yyyymm(now))
    raw = await redis.get(key)
    current_count: int = int(raw) if raw is not None else 0

    over_cap = current_count >= period_cap
    soft_threshold = int(period_cap * SOFT_CAP_PCT)
    at_soft_cap = current_count >= soft_threshold

    return QuotaStatus(
        over_cap=over_cap,
        at_soft_cap=at_soft_cap,
        current_count=current_count,
        period_cap=period_cap,
    )


async def increment_counter(
    redis: redis_async.Redis,
    *,
    tenant_id: UUID,
    now: datetime | None = None,
) -> int:
    """Increment the reply counter for the current period.

    Returns the new counter value after the increment.  Called at pipeline
    step 12b after a reply has been successfully dispatched.

    The TTL is refreshed on every increment so the key survives for 35 days
    after the last reply, giving the nightly flush ample time to drain it.
    """
    key = _redis_key(tenant_id, period_yyyymm(now))
    new_val: int = await redis.incr(key)
    await redis.expire(key, _KEY_TTL_SECONDS)
    return new_val


# ── flush-facing helper ───────────────────────────────────────────────────────


async def get_all_active_counters(
    redis: redis_async.Redis,
    *,
    period_yyyymm: str,
) -> dict[str, int]:
    """SCAN Redis for all bot-reply keys matching the given period.

    Returns ``{tenant_id_str: count}``.  Used by the nightly
    ``scheduled/quota_flush.py`` job; never called from the hot path.

    The SCAN pattern ``bot_replies:*:{period}`` avoids a full keyspace scan
    on clusters with many periods.  Keys with count=0 are skipped.
    """
    pattern = f"bot_replies:*:{period_yyyymm}"
    result: dict[str, int] = {}

    cursor: int = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=200)
        for key_bytes in keys:
            key_str: str = (
                key_bytes.decode("utf-8")
                if isinstance(key_bytes, bytes)
                else key_bytes
            )
            raw = await redis.get(key_str)
            if raw is None:
                continue
            count = int(raw)
            if count <= 0:
                continue
            # Key format: bot_replies:{tenant_id}:{period}
            parts = key_str.split(":")
            if len(parts) != 3:  # noqa: PLR2004
                logger.warning("quota.get_all_active_counters: unexpected key %s", key_str)
                continue
            result[parts[1]] = count  # tenant_id string
        if cursor == 0:
            break

    return result
