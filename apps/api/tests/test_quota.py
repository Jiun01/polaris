"""Bot-replies quota service tests — Phase 10 PR-1."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.quota import (
    QuotaStatus,
    _redis_key,
    check_quota,
    get_all_active_counters,
    increment_counter,
    period_yyyymm,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_conn(cap: int = 5000, plan_tier: str = "starter") -> Any:
    """Return a mock asyncpg connection that returns a quota row."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "bot_replies_period_cap": cap,
        "plan_tier": plan_tier,
    }
    return conn


def _fake_redis(current_count: int = 0, *, cached_cap: int | None = None) -> Any:
    """Return a mock redis client with the given counter value.

    audit Batch B (B5): ``check_quota`` now also does a ``redis.get`` for
    the ``quota_cap:{tenant_id}`` cache BEFORE the counter read, so ``.get``
    must key-discriminate rather than return one canned value for every
    call. ``cached_cap=None`` (default) means every ``quota_cap:*`` lookup
    is a miss — the existing DB-cap-read tests exercise the un-cached path
    unchanged; ``cached_cap=N`` simulates a warm cache for the dedicated
    cache-hit test below.
    """
    import json as _json

    r = AsyncMock()
    counter_value = str(current_count).encode() if current_count > 0 else None

    async def get(key: str) -> bytes | None:
        if key.startswith("quota_cap:"):
            if cached_cap is None:
                return None
            return _json.dumps({"period_cap": cached_cap}).encode()
        return counter_value

    r.get = AsyncMock(side_effect=get)
    r.set = AsyncMock(return_value=True)
    r.incr.return_value = current_count + 1
    r.expire.return_value = True
    return r


# ── _period_yyyymm ────────────────────────────────────────────────────────────


async def test_period_yyyymm_format() -> None:
    from datetime import UTC, datetime
    dt = datetime(2026, 5, 22, tzinfo=UTC)
    assert period_yyyymm(dt) == "202605"


# ── _redis_key ────────────────────────────────────────────────────────────────


async def test_redis_key_format() -> None:
    tid = uuid4()
    key = _redis_key(tid, "202605")
    assert key == f"bot_replies:{tid}:202605"


# ── check_quota ───────────────────────────────────────────────────────────────


async def test_check_quota_under_cap() -> None:
    conn = _fake_conn(cap=5000)
    redis = _fake_redis(current_count=100)
    status = await check_quota(conn, redis, tenant_id=uuid4())
    assert isinstance(status, QuotaStatus)
    assert status.over_cap is False
    assert status.at_soft_cap is False
    assert status.current_count == 100
    assert status.period_cap == 5000


async def test_check_quota_at_soft_cap() -> None:
    conn = _fake_conn(cap=5000)
    redis = _fake_redis(current_count=4000)  # 80 % of 5000
    status = await check_quota(conn, redis, tenant_id=uuid4())
    assert status.over_cap is False
    assert status.at_soft_cap is True


async def test_check_quota_over_cap() -> None:
    conn = _fake_conn(cap=5000)
    redis = _fake_redis(current_count=5001)
    status = await check_quota(conn, redis, tenant_id=uuid4())
    assert status.over_cap is True
    assert status.current_count == 5001
    assert status.period_cap == 5000


async def test_check_quota_exactly_at_cap_is_over() -> None:
    conn = _fake_conn(cap=5000)
    redis = _fake_redis(current_count=5000)
    status = await check_quota(conn, redis, tenant_id=uuid4())
    assert status.over_cap is True


async def test_check_quota_zero_count_never_over() -> None:
    conn = _fake_conn(cap=5000)
    redis = _fake_redis(current_count=0)
    status = await check_quota(conn, redis, tenant_id=uuid4())
    assert status.over_cap is False
    assert status.current_count == 0


async def test_check_quota_missing_quota_row_uses_starter_default() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None  # no quota row
    redis = _fake_redis(current_count=0)
    status = await check_quota(conn, redis, tenant_id=uuid4())
    assert status.period_cap == 5_000  # starter default
    assert status.over_cap is False


async def test_check_quota_cap_cache_miss_writes_cache() -> None:
    """audit Batch B (B5): a cold cap cache reads the DB AND writes the
    cache key for next time."""
    conn = _fake_conn(cap=25_000)
    redis = _fake_redis(current_count=0)  # cached_cap=None → cache miss
    tenant_id = uuid4()

    status = await check_quota(conn, redis, tenant_id=tenant_id)

    assert status.period_cap == 25_000
    conn.fetchrow.assert_awaited_once()
    redis.set.assert_awaited_once()
    set_call = redis.set.call_args
    assert set_call.args[0] == f"quota_cap:{tenant_id}"
    assert set_call.kwargs["ex"] == 60


async def test_check_quota_cap_cached_skips_db() -> None:
    """audit Batch B (B5): a warm cap cache skips the tenant_quotas fetchrow
    entirely — the second `check_quota` call within 60s costs zero DB reads."""
    conn = _fake_conn(cap=5_000)
    redis = _fake_redis(current_count=100, cached_cap=25_000)
    tenant_id = uuid4()

    status = await check_quota(conn, redis, tenant_id=tenant_id)

    # The CACHED value (25_000) wins, not the DB fixture's 5_000 — proving
    # the DB read never ran.
    assert status.period_cap == 25_000
    conn.fetchrow.assert_not_called()
    redis.set.assert_not_called()  # already cached; no redundant write


# ── increment_counter ─────────────────────────────────────────────────────────


async def test_increment_counter_returns_new_value() -> None:
    redis = _fake_redis(current_count=99)
    new_val = await increment_counter(redis, tenant_id=uuid4())
    assert new_val == 100
    redis.incr.assert_called_once()
    redis.expire.assert_called_once()


async def test_increment_counter_calls_expire_with_35_day_ttl() -> None:
    redis = _fake_redis()
    await increment_counter(redis, tenant_id=uuid4())
    call_args = redis.expire.call_args
    ttl = call_args[0][1]
    assert ttl == 86_400 * 35


# ── get_all_active_counters ───────────────────────────────────────────────────


async def test_get_all_active_counters_returns_parsed_map() -> None:
    tid1 = uuid4()
    tid2 = uuid4()
    period = "202605"

    redis = AsyncMock()
    # scan returns all keys in one pass (cursor=0 means done)
    redis.scan.return_value = (
        0,
        [
            f"bot_replies:{tid1}:{period}".encode(),
            f"bot_replies:{tid2}:{period}".encode(),
        ],
    )
    redis.get.side_effect = [b"150", b"3200"]

    result = await get_all_active_counters(redis, period_yyyymm=period)
    assert result[str(tid1)] == 150
    assert result[str(tid2)] == 3200


async def test_get_all_active_counters_skips_zero_counts() -> None:
    tid = uuid4()
    period = "202605"

    redis = AsyncMock()
    redis.scan.return_value = (0, [f"bot_replies:{tid}:{period}".encode()])
    redis.get.return_value = b"0"

    result = await get_all_active_counters(redis, period_yyyymm=period)
    assert result == {}
