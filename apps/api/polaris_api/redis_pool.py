"""Redis connection singleton — Phase 2 PR-1.

One `redis.asyncio.Redis` client per process, opened in the FastAPI lifespan
and closed at shutdown. Mirrors the asyncpg dual-pool lifecycle in
``polaris_api.db.pools``.

Why a singleton:

- ``redis.asyncio.Redis`` already manages its own connection pool internally;
  constructing a new client per request defeats that. We want one client that
  multiplexes commands across an internal connection pool, just like asyncpg.
- Lifespan-scoped open/close lets us fail fast at boot if the env is
  misconfigured, and guarantees a clean shutdown without leaked sockets.

Usage from Phase 2+ request handlers::

    from polaris_api.redis_pool import get_redis

    async def handler():
        redis = get_redis()
        await redis.set("polaris:webhook:dedup:meta:<wamid>", "1", nx=True, ex=604800)

Name note: this module is intentionally a single file (not a ``redis/``
subpackage) so it can never shadow the top-level ``redis`` PyPI package on a
relative-import slip. We import the third-party package as
``redis.asyncio as redis_async`` and never write ``from . import …`` inside
this file.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis_async

from polaris_api.config import get_settings

logger = logging.getLogger(__name__)

_client: redis_async.Redis | None = None

# Timeouts in seconds. Two distinct knobs:
#   - socket_connect_timeout: time to establish the TCP/TLS handshake
#   - socket_timeout:         per-command read/write timeout once connected
# A webhook handler that gets a 2s connect timeout but no per-command timeout
# would still block forever on a command if redis hangs mid-stream; both are
# load-bearing.
_CONNECT_TIMEOUT_S = 2.0
_COMMAND_TIMEOUT_S = 2.0


async def init_redis() -> None:
    """Open the singleton client. Called from ``main.py`` lifespan startup.

    Does **not** ping — ``redis.from_url`` is lazy; the first command opens
    the underlying connection. Pinging at boot would couple lifespan startup
    to Redis availability, which we don't want: ``/v1/health`` should still
    answer when Redis is down (the probe reports it as unreachable) so an
    operator can read the health endpoint to diagnose.

    Idempotency note: re-calling raises rather than silently returning,
    same as ``init_pools``. Hides lifespan double-start bugs.
    """
    global _client  # noqa: PLW0603 — module-level singleton by design
    if _client is not None:
        raise RuntimeError("init_redis called twice — lifespan double-start?")

    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError(
            "REDIS_URL is unset. Set it in .env (see .env.example) before booting."
        )

    # redis-py 5's `from_url` is untyped; mypy ignore is local + narrow.
    _client = redis_async.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url,
        socket_connect_timeout=_CONNECT_TIMEOUT_S,
        socket_timeout=_COMMAND_TIMEOUT_S,
        decode_responses=False,
    )
    logger.info("redis client initialised")


async def close_redis() -> None:
    """Drain + close. Called from ``main.py`` lifespan shutdown.

    Best-effort: cleanup must not raise into shutdown, which would otherwise
    obscure the underlying reason the process is stopping. We log + swallow.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception as exc:  # noqa: BLE001 — cleanup must not raise
        logger.warning("redis close failed: %s", type(exc).__name__)
        logger.debug("redis close error detail: %s", exc)
    _client = None


def get_redis() -> redis_async.Redis:
    """Return the singleton Redis client. Raises before ``init_redis()`` runs.

    Synchronous because the client is already constructed; nothing to await
    here. FastAPI deps wrap this call from an async function when needed.
    """
    if _client is None:
        raise RuntimeError("redis client not initialised — call init_redis() first")
    return _client
