"""Inbound worker loop tests — Phase 4 PR-4.

Verifies the worker:
  - calls pipeline.run on each queued IncomingMessage
  - ACKs each message via queue.task_done() even on exception
  - swallows LLMConfigError / LLMHardFailure / ValueError / generic
    Exception so a bad message can't kill the worker
  - propagates CancelledError for clean lifespan shutdown
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _make_incoming() -> Any:
    from polaris_shared_types import IncomingMessage, SenderRef

    return IncomingMessage(
        tenant_id=uuid4(),
        channel_id=uuid4(),
        provider="whatsapp",
        provider_message_id="wamid.test",
        sender=SenderRef(channel_user_id="60123", display_name=None),
        body="hi",
        received_at=datetime.now(UTC),
    )


def _fake_deps() -> Any:
    """Minimal PipelineDeps stand-in for the worker — pipeline.run is mocked
    so the deps internals never get exercised."""
    from polaris_api.services.pipeline import PipelineDeps

    return PipelineDeps(
        anon_pool=MagicMock(), redis=MagicMock(), meta_client=MagicMock(),
    )


async def test_worker_calls_pipeline_per_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polaris_api.services import inbound_queue, inbound_worker, pipeline
    from polaris_shared_types import PipelineDecision, ResponseEnvelope, UsageMetrics

    inbound_queue.reset_queue_for_tests()
    queue = inbound_queue.get_queue()

    msg = _make_incoming()
    await queue.put(msg)

    captured: list = []

    async def fake_run(*, incoming, deps):
        captured.append(incoming)
        return PipelineDecision(
            conversation_id=uuid4(), tenant_id=incoming.tenant_id,
            user_message_id=uuid4(), assistant_message_id=uuid4(),
            response=ResponseEnvelope(
                content="ok", intent="simple", model_used="haiku",
                language_detected="en",
                usage=UsageMetrics(input_tokens=10, output_tokens=2),
                latency_ms=5,
            ),
        )

    monkeypatch.setattr(pipeline, "run_inbound",fake_run)

    task = asyncio.create_task(inbound_worker.run_worker(_fake_deps()))
    # Give the worker time to drain.
    await asyncio.wait_for(queue.join(), timeout=2.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(captured) == 1
    assert captured[0].tenant_id == msg.tenant_id


async def test_worker_survives_llm_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Placeholder Anthropic key → log + ACK + keep running."""
    from polaris_api.services import inbound_queue, inbound_worker, pipeline
    from polaris_api.services._llm_models import LLMConfigError

    inbound_queue.reset_queue_for_tests()
    queue = inbound_queue.get_queue()
    await queue.put(_make_incoming())
    await queue.put(_make_incoming())

    call_count = 0

    async def fake_run(*, incoming, deps):
        nonlocal call_count
        call_count += 1
        raise LLMConfigError("ANTHROPIC_API_KEY placeholder")

    monkeypatch.setattr(pipeline, "run_inbound",fake_run)

    task = asyncio.create_task(inbound_worker.run_worker(_fake_deps()))
    await asyncio.wait_for(queue.join(), timeout=2.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Both messages drained even though every pipeline.run raised.
    assert call_count == 2


async def test_worker_survives_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception class still drains the queue."""
    from polaris_api.services import inbound_queue, inbound_worker, pipeline

    inbound_queue.reset_queue_for_tests()
    queue = inbound_queue.get_queue()
    await queue.put(_make_incoming())

    async def boom(*, incoming, deps):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(pipeline, "run_inbound",boom)

    task = asyncio.create_task(inbound_worker.run_worker(_fake_deps()))
    await asyncio.wait_for(queue.join(), timeout=2.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert queue.empty()


async def test_worker_rejects_chatwoot_provider_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError from pipeline (e.g. chatwoot provider) is swallowed."""
    from polaris_api.services import inbound_queue, inbound_worker, pipeline

    inbound_queue.reset_queue_for_tests()
    queue = inbound_queue.get_queue()
    # Worker just calls pipeline.run; the rejection happens inside it.
    await queue.put(_make_incoming())

    async def reject(*, incoming, deps):
        raise ValueError("chatwoot rejected")

    monkeypatch.setattr(pipeline, "run_inbound",reject)

    task = asyncio.create_task(inbound_worker.run_worker(_fake_deps()))
    await asyncio.wait_for(queue.join(), timeout=2.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_worker_cancel_propagates_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation surfaces as CancelledError, not swallowed."""
    from polaris_api.services import inbound_queue, inbound_worker

    inbound_queue.reset_queue_for_tests()

    task = asyncio.create_task(inbound_worker.run_worker(_fake_deps()))
    # Let the worker get into the queue.get() await.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
