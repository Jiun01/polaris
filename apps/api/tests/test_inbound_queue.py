"""Phase 2 PR-4 — in-process IncomingMessage queue.

DB-free. Exercises the singleton lifecycle + enqueue / consumer
contract that Phase 4's LLM handler will replace.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from polaris_api.services import inbound_queue
from polaris_shared_types import IncomingMessage, SenderRef


def _sample_message() -> IncomingMessage:
    return IncomingMessage(
        tenant_id=uuid4(),
        channel_id=uuid4(),
        provider="whatsapp",
        provider_message_id=f"wamid.{uuid4().hex[:8]}",
        sender=SenderRef(channel_user_id="60123456789"),
        body="test body",
        attachments=[],
        received_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _reset_queue() -> None:
    """Each test starts with a fresh queue. The singleton is reset on
    both sides of the test so a queue created under a different event
    loop in a sibling test doesn't leak in."""
    inbound_queue.reset_queue_for_tests()
    yield
    inbound_queue.reset_queue_for_tests()


@pytest.mark.asyncio
async def test_enqueue_and_get() -> None:
    msg = _sample_message()
    await inbound_queue.enqueue(msg)

    queue = inbound_queue.get_queue()
    out = await queue.get()
    assert out is msg
    queue.task_done()


@pytest.mark.asyncio
async def test_multiple_enqueues_preserve_order() -> None:
    messages = [_sample_message() for _ in range(5)]
    for m in messages:
        await inbound_queue.enqueue(m)

    queue = inbound_queue.get_queue()
    for original in messages:
        received = await queue.get()
        assert received is original
        queue.task_done()


@pytest.mark.asyncio
async def test_get_queue_returns_singleton() -> None:
    q1 = inbound_queue.get_queue()
    q2 = inbound_queue.get_queue()
    assert q1 is q2


@pytest.mark.asyncio
async def test_enqueue_returns_true_on_success() -> None:
    """silent-failure audit fix A6 (2026-07-02): enqueue is now non-blocking
    (put_nowait) and returns a bool the caller must check."""
    msg = _sample_message()
    assert await inbound_queue.enqueue(msg) is True


@pytest.mark.asyncio
async def test_enqueue_returns_false_when_queue_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full queue must not block the caller — enqueue returns False
    immediately so the webhook router can release the idempotency claim and
    503 for a provider retry."""
    monkeypatch.setattr(inbound_queue, "_QUEUE_MAX_SIZE", 1)
    inbound_queue.reset_queue_for_tests()
    queue = inbound_queue.get_queue()
    # Fill the queue to capacity with a raw put_nowait (bypass enqueue).
    queue.put_nowait(_sample_message())  # type: ignore[arg-type]

    assert await inbound_queue.enqueue(_sample_message()) is False


@pytest.mark.asyncio
async def test_logging_consumer_drains_queue() -> None:
    """The placeholder consumer must drain messages, not block them.
    Phase 4 will replace this with the LLM handler; this test guards
    against a regression where the consumer silently stops on the
    first exception."""
    msg = _sample_message()
    task = asyncio.create_task(inbound_queue._logging_consumer())
    try:
        await inbound_queue.enqueue(msg)
        queue = inbound_queue.get_queue()
        # Wait for the consumer to call task_done. join() blocks until
        # all enqueued tasks are marked done.
        await asyncio.wait_for(queue.join(), timeout=1.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
