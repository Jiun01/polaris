"""Redis-backed webhook idempotency — Phase 2 PR-4.

A single ``claim`` coroutine that wraps Redis ``SET key value NX EX ttl``
into a clear boolean: ``True`` on first-write-wins (this delivery is
new), ``False`` on conflict (this is a re-delivery — skip the rest of
the pipeline).

Key shape: ``polaris:webhook:dedup:{source}:{external_id}``. Namespaced
so future workloads under the same Redis instance don't collide.

TTL: 7 days by default per the phase checklist. Meta retries failed
deliveries for up to 7 days (then dead-letters them on their side); a
TTL shorter than that lets a late retry slip through as "new."

Why a thin wrapper rather than inlining the SET call:

1. ``redis.asyncio.Redis.set(..., nx=True)`` returns ``None`` on
   conflict, NOT ``False``. ``if not result:`` works; ``if result is
   False:`` is a silent bug that lets every replay through. The wrapper
   normalises to a clean ``bool``.
2. Centralises the key-shape contract — Phase 4's broadcast worker +
   Phase 7's handoff router will reuse the same shape so dedup namespaces
   stay coherent.
3. The webhook router stays free of Redis import boilerplate; tests
   inject a fake via FastAPI dep override or direct attribute patch.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis_async

logger = logging.getLogger(__name__)

# 7 days. Matches the phase checklist ("dedup by (provider,
# provider_message_id) for 7 days via Redis SETNX with TTL"). Meta's
# retry window for failed deliveries is also 7 days, so a late retry
# of an already-acked delivery still dedupes.
_DEFAULT_TTL_S = 7 * 24 * 60 * 60


def _key(source: str, external_id: str) -> str:
    return f"polaris:webhook:dedup:{source}:{external_id}"


async def claim(
    redis: redis_async.Redis,
    source: str,
    external_id: str,
    *,
    ttl_s: int = _DEFAULT_TTL_S,
) -> bool:
    """Try to claim ``(source, external_id)`` for processing.

    Returns ``True`` if this is the first time we've seen the pair —
    caller should proceed with the rest of the dispatch pipeline.
    Returns ``False`` if Redis already had the key (a re-delivery) —
    caller should record a ``replay_ignored`` row and skip downstream
    side effects.

    Network errors from Redis surface as ``redis.RedisError`` and are
    NOT caught here. The router maps them to HTTP 503 so Meta retries
    the delivery — the next attempt will redo the claim cleanly.
    """
    key = _key(source, external_id)
    # `redis.set(..., nx=True, ex=ttl)` returns:
    #   True  when the key was set (first-write-wins)
    #   None  when the key already existed (conflict)
    # We normalise None → False so the caller can branch on a plain bool.
    result = await redis.set(key, b"1", nx=True, ex=ttl_s)
    if result is True:
        return True
    if result is None:
        logger.info("idempotency claim conflict: %s %s", source, external_id)
        return False
    # Defensive: a future redis-py version returning some other truthy
    # value (e.g. b"OK") shouldn't accidentally fail-closed. Treat any
    # non-None response as "key was set."
    return bool(result)


async def release(
    redis: redis_async.Redis,
    source: str,
    external_id: str,
) -> None:
    """Delete a previously-claimed ``(source, external_id)`` key.

    Call this to UNDO a ``claim`` when the work it guarded could not be handed
    off durably — so the provider's retry of the same delivery re-processes
    instead of deduping as a ``replay_ignored`` (which would silently lose the
    work). Used by the Chatwoot email branch when the email-assist queue is full
    and drops the job: without releasing, the retry hits the still-set key and
    the customer email never gets a draft.

    Best-effort by contract: a missing key is a no-op (Redis ``DEL`` returns 0).
    Network errors surface as ``redis.RedisError`` and are NOT caught here — the
    caller already maps Redis outages to HTTP 503 (which itself triggers a
    retry), so the claim-release race resolves on the next delivery either way.
    """
    await redis.delete(_key(source, external_id))
