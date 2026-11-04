"""Phase 2 PR-1 — Redis singleton lifecycle.

Three invariants:

1. ``get_redis()`` raises ``RuntimeError`` before ``init_redis()``.
2. After ``init_redis()`` it returns a usable ``redis.asyncio.Redis``.
3. After ``close_redis()`` it raises again — no stale handle.

We don't require a running Redis to run this test: the underlying client is
lazy (connection opens on first command). The test exercises the singleton's
lifecycle state transitions only.
"""

from __future__ import annotations

import pytest
from polaris_api import redis_pool


@pytest.mark.asyncio
async def test_get_redis_raises_before_init() -> None:
    # Defensive: prior tests may have left the singleton open. Reset cleanly.
    await redis_pool.close_redis()

    with pytest.raises(RuntimeError, match="not initialised"):
        redis_pool.get_redis()


@pytest.mark.asyncio
async def test_get_redis_returns_client_after_init() -> None:
    await redis_pool.close_redis()  # idempotent reset
    await redis_pool.init_redis()
    try:
        client = redis_pool.get_redis()
        # Don't ping (no live Redis required for this test); just assert the
        # client surface is correct.
        assert hasattr(client, "set")
        assert hasattr(client, "get")
        assert hasattr(client, "aclose")
    finally:
        await redis_pool.close_redis()


@pytest.mark.asyncio
async def test_init_redis_twice_raises() -> None:
    await redis_pool.close_redis()
    await redis_pool.init_redis()
    try:
        with pytest.raises(RuntimeError, match="double-start"):
            await redis_pool.init_redis()
    finally:
        await redis_pool.close_redis()


@pytest.mark.asyncio
async def test_get_redis_raises_after_close() -> None:
    await redis_pool.close_redis()
    await redis_pool.init_redis()
    await redis_pool.close_redis()
    with pytest.raises(RuntimeError, match="not initialised"):
        redis_pool.get_redis()


@pytest.mark.asyncio
async def test_close_redis_idempotent() -> None:
    """Calling close twice must not raise — lifespan shutdown calls it
    unconditionally."""
    await redis_pool.close_redis()
    await redis_pool.close_redis()  # second call is a no-op
