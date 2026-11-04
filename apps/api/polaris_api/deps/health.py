"""Health-probe helpers — exercised by /health.

Phase 0 stance: best-effort probes that return a bool + don't raise. Phase 1 will
replace these with real pool checks (asyncpg + redis-py client).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
import redis.asyncio as redis_async

from polaris_api.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProbeResult:
    """Outcome of a single dependency probe."""

    reachable: bool
    detail: str | None = None


async def probe_supabase(timeout_s: float = 2.0) -> ProbeResult:
    """HEAD the Supabase URL. Anything < 500 counts as reachable."""
    settings = get_settings()
    url = settings.supabase_url
    if not url or "placeholder" in url:
        return ProbeResult(reachable=False, detail="not configured")
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.head(url)
            if resp.status_code < 500:
                return ProbeResult(reachable=True)
            return ProbeResult(reachable=False, detail=f"http {resp.status_code}")
    except Exception as exc:  # noqa: BLE001 — health-probe must not raise
        # Log only the exception type name. `str(exc)` from httpx/asyncpg
        # routinely includes the target URL, which can contain credentials
        # if a future env misconfig puts them in SUPABASE_URL. Full detail
        # goes to DEBUG only.
        logger.warning("supabase probe failed: %s", type(exc).__name__)
        logger.debug("supabase probe error detail: %s", exc)
        return ProbeResult(reachable=False, detail=type(exc).__name__)


async def probe_redis(timeout_s: float = 2.0) -> ProbeResult:
    """PING redis. Reachable iff PING returns PONG within timeout."""
    settings = get_settings()
    url = settings.redis_url
    if not url:
        return ProbeResult(reachable=False, detail="not configured")
    client: redis_async.Redis | None = None
    try:
        # redis-py's `from_url` is untyped in v5; revisit when redis>=6 ships stubs.
        client = redis_async.from_url(url, socket_timeout=timeout_s)  # type: ignore[no-untyped-call]
        pong = await client.ping()
        # Use truthiness, not `is True` — defensive against future redis-py
        # versions returning `1` or another truthy value.
        if pong:
            return ProbeResult(reachable=True)
        return ProbeResult(reachable=False, detail="ping returned falsy")
    except Exception as exc:  # noqa: BLE001
        # Same credential-leak guard as probe_supabase — log type name only.
        logger.warning("redis probe failed: %s", type(exc).__name__)
        logger.debug("redis probe error detail: %s", exc)
        return ProbeResult(reachable=False, detail=type(exc).__name__)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 — cleanup must not raise
                logger.debug("redis client cleanup raised: %s", exc)
