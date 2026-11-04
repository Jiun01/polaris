"""Phase 2 PR-4 — Redis SETNX idempotency primitive.

DB-free. Uses ``fakeredis`` (configured in conftest's ``fake_redis``
fixture) so CI runs without a live Redis.

Asserted behaviour:
  - First ``claim`` for a fresh key returns ``True``.
  - Second ``claim`` for the same key returns ``False`` (no
    side-effects observable; the second call must not silently 'fix'
    the conflict).
  - TTL is set so the key expires; a claim after expiry returns
    ``True`` again.
  - Distinct keys never collide (per-source + per-external_id
    namespace).
  - Normalisation: redis-py 5's ``set(..., nx=True)`` returns ``None``
    on conflict; the wrapper turns that into a Python ``bool`` so a
    caller writing ``if not is_new:`` works correctly.
"""

from __future__ import annotations

import pytest
import redis.asyncio as redis_async
from polaris_api.services.idempotency import claim


@pytest.mark.asyncio
async def test_first_claim_returns_true(fake_redis: redis_async.Redis) -> None:
    result = await claim(fake_redis, "meta", "wamid.first-claim-test")
    assert result is True


@pytest.mark.asyncio
async def test_second_claim_returns_false(fake_redis: redis_async.Redis) -> None:
    first = await claim(fake_redis, "meta", "wamid.replay-test")
    second = await claim(fake_redis, "meta", "wamid.replay-test")
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_distinct_external_ids_do_not_collide(
    fake_redis: redis_async.Redis,
) -> None:
    a = await claim(fake_redis, "meta", "wamid.a")
    b = await claim(fake_redis, "meta", "wamid.b")
    assert a is True
    assert b is True


@pytest.mark.asyncio
async def test_distinct_sources_do_not_collide(fake_redis: redis_async.Redis) -> None:
    """Same external_id under different sources is two distinct claims —
    a Meta wamid and a Chatwoot message id could in theory collide as
    strings, but the source namespace prevents it."""
    meta = await claim(fake_redis, "meta", "shared-id")
    chatwoot = await claim(fake_redis, "chatwoot", "shared-id")
    assert meta is True
    assert chatwoot is True


@pytest.mark.asyncio
async def test_ttl_expires_lets_claim_succeed_again(
    fake_redis: redis_async.Redis,
) -> None:
    """After the TTL elapses the key is gone, so a later claim returns
    True. fakeredis' ``time`` mocking is awkward; we test the
    behaviour by manually deleting the key (TTL-driven deletion is a
    Redis-side concern, not something to re-test here)."""
    first = await claim(fake_redis, "meta", "ttl-key", ttl_s=60)
    second = await claim(fake_redis, "meta", "ttl-key", ttl_s=60)
    assert first is True
    assert second is False

    # Simulate TTL expiry.
    await fake_redis.delete("polaris:webhook:dedup:meta:ttl-key")

    third = await claim(fake_redis, "meta", "ttl-key", ttl_s=60)
    assert third is True


@pytest.mark.asyncio
async def test_ttl_is_actually_set(fake_redis: redis_async.Redis) -> None:
    """A claim without TTL would be a memory leak — the key would
    persist indefinitely. Assert Redis sees a positive TTL."""
    await claim(fake_redis, "meta", "ttl-existence-test", ttl_s=3600)
    ttl = await fake_redis.ttl("polaris:webhook:dedup:meta:ttl-existence-test")
    assert ttl > 0
    assert ttl <= 3600
