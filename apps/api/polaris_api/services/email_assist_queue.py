"""In-process email-assist job queue — email channel M2.

Mirror of ``services/inbound_queue.py`` for the email-assist path. The Chatwoot
router enqueues an ``EmailAssistJob`` when an inbound resolves to
``channel='email'``; ``email_assist_worker`` drains it and posts the draft note.

Kept SEPARATE from ``inbound_queue`` on purpose: the assist flow must never touch
the customer-reply pipeline (which rejects ``provider='chatwoot'`` at
``pipeline.py:157``). A dedicated queue keeps the two paths fully isolated.

Bounded (``maxsize=1000``) so a stalled worker applies backpressure rather than
memory-bloating, same posture as the inbound queue.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polaris_api.services.email_assist import EmailAssistJob

logger = logging.getLogger(__name__)

_QUEUE_MAX_SIZE = 1000
_queue: asyncio.Queue[EmailAssistJob] | None = None


def get_queue() -> asyncio.Queue[EmailAssistJob]:
    """Return the singleton queue. Lazily constructs on first call (building an
    ``asyncio.Queue`` doesn't need a running loop until put/get)."""
    global _queue  # noqa: PLW0603 — module-level singleton by design
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
    return _queue


async def enqueue(job: EmailAssistJob) -> bool:
    """Offer ``job`` to the queue WITHOUT blocking the caller.

    The Chatwoot webhook request coroutine calls this, so it must not suspend on
    a full queue — a stalled LLM worker would otherwise hang every email webhook
    until Chatwoot times out and retries, compounding the backlog.

    Returns ``True`` when the job was accepted, ``False`` when the queue was full
    and the job was dropped. The caller MUST treat a ``False`` as "not processed"
    and make the delivery retryable (release the idempotency claim + 503 so
    Chatwoot redelivers) — otherwise the customer email gets no draft and the
    replay is silently ignored. See ``chatwoot_router`` email branch."""
    try:
        get_queue().put_nowait(job)
        return True
    except asyncio.QueueFull:
        logger.warning(
            "email_assist_queue: full (maxsize=%d) — dropping job tenant=%s conv=%s",
            _QUEUE_MAX_SIZE,
            job.tenant_id,
            job.chatwoot_conversation_id,
        )
        return False


def reset_queue_for_tests() -> None:
    """Test-only: drop the singleton so the next ``get_queue()`` rebuilds a
    fresh queue bound to the current event loop. Production never calls this."""
    global _queue  # noqa: PLW0603
    _queue = None
