"""In-process inbound message queue — Phase 2 PR-4.

A single bounded ``asyncio.Queue[IncomingMessage]`` per worker process.
Phase 2 ships:

  - the queue + ``enqueue`` helper (this module),
  - a placeholder logging consumer started in the FastAPI lifespan
    so the queue never grows unbounded in dev / staging.

Phase 4 replaces the logging consumer with the LLM-driven message
handler. Phase 7 may add a second consumer for handoff state
transitions. The queue lets multiple consumers fan out without the
dispatcher knowing about any of them.

Why in-process for GA (vs an external broker):

  - Single-pod GA. Horizontal scale lands with Phase 6's broadcast
    worker; revisit then (Q-PHASE2-02 in 08_OPEN_QUESTIONS.md).
  - Latency profile: a webhook → LLM reply round-trip is target ≤2s
    per ADR-031; an in-process queue adds <1ms overhead. An external
    broker (Redis Streams, Kafka) would add 5-20ms each direction
    without buying us anything at single-pod scale.
  - Crash safety: if the pod dies mid-process, Meta retries the
    delivery for up to 7 days; the idempotency layer in
    ``services/idempotency.py`` deduplicates the redelivery. Durable
    queueing would be over-engineering for GA's volume.

Bounded queue (``maxsize=1000``) so a stalled consumer applies backpressure.
``enqueue`` uses ``put_nowait`` and returns ``False`` on a full queue (silent-
failure audit fix A6, 2026-07-02) — the caller 503s so the provider retries,
rather than blocking the request coroutine indefinitely on a stalled
consumer. The threshold is conservative — at peak Phase 4 loads we expect
<50 in-flight items.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polaris_shared_types import IncomingMessage

logger = logging.getLogger(__name__)

_QUEUE_MAX_SIZE = 1000
_queue: asyncio.Queue[IncomingMessage] | None = None


def get_queue() -> asyncio.Queue[IncomingMessage]:
    """Return the singleton queue. Lazily constructs on first call.

    Synchronous — building an ``asyncio.Queue`` doesn't require an
    event loop until you actually ``put`` / ``get`` on it.
    """
    global _queue  # noqa: PLW0603 — module-level singleton by design
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
    return _queue


async def enqueue(message: IncomingMessage) -> bool:
    """Offer ``message`` to the queue WITHOUT blocking the caller.

    The webhook router runs this from within the request handler, so it must
    not suspend on a full queue — a stalled consumer would otherwise hang
    every inbound webhook request until the provider's own delivery timeout
    fires, compounding the backlog instead of applying fast backpressure.
    Mirrors ``services/email_assist_queue.py::enqueue`` exactly.

    Returns ``True`` when the message was accepted, ``False`` when the queue
    was full and the message was dropped. The caller MUST treat a ``False``
    as "not processed" and make the delivery retryable (release the
    idempotency claim + 503 so the provider redelivers) — otherwise the
    inbound message gets no reply and the retry is silently ignored as a
    replay. See ``webhooks/chatwoot_router.py``'s bot-provider branch.
    """
    try:
        get_queue().put_nowait(message)
        return True
    except asyncio.QueueFull:
        logger.warning(
            "inbound_queue: full (maxsize=%d) — dropping message tenant=%s "
            "channel=%s provider_message_id=%s",
            _QUEUE_MAX_SIZE,
            message.tenant_id,
            message.channel_id,
            message.provider_message_id,
        )
        return False


async def _logging_consumer() -> None:
    """Placeholder consumer started in the FastAPI lifespan.

    Phase 4 will replace this with the LLM-driven handler. Until then
    we drain the queue and log a single info line per message so a
    backed-up queue is visible during development without crashing
    the dispatcher.

    Survives consumer-side exceptions: any ``Exception`` from a single
    iteration is logged + the loop continues. Cancellation
    (``asyncio.CancelledError``) propagates so lifespan shutdown is
    clean.
    """
    queue = get_queue()
    logger.info("inbound_queue: logging consumer started")
    try:
        while True:
            message = await queue.get()
            try:
                # Log only the structural identifiers — body / sender
                # may carry PII; the dispatcher logs envelope shape
                # separately at INFO. Per-tenant body inspection is a
                # Phase 9 admin surface, not a default ops log.
                logger.info(
                    "inbound_queue: tenant=%s channel=%s provider=%s "
                    "provider_message_id=%s body_present=%s attachments=%d",
                    message.tenant_id,
                    message.channel_id,
                    message.provider,
                    message.provider_message_id,
                    message.body is not None,
                    len(message.attachments),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "inbound_queue: consumer iteration failed: %s",
                    type(exc).__name__,
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("inbound_queue: logging consumer stopping")
        raise


def reset_queue_for_tests() -> None:
    """Test-only helper. Drops the singleton so a subsequent
    ``get_queue()`` rebuilds a fresh queue tied to the current event
    loop. Production code never calls this."""
    global _queue  # noqa: PLW0603
    _queue = None
