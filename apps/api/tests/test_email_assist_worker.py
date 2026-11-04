"""Worker-loop tests for email_assist_worker — email channel M2.

Mirrors test_inbound_worker: the worker drains the queue, calls the handler,
and survives a handler exception without dying. No DB — the handler + singletons
are stubbed.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from polaris_api.services import email_assist, email_assist_worker
from polaris_api.services.email_assist import EmailAssistJob
from polaris_api.services.email_assist_queue import (
    enqueue,
    get_queue,
    reset_queue_for_tests,
)

pytestmark = pytest.mark.asyncio


def _job() -> EmailAssistJob:
    return EmailAssistJob(
        tenant_id=uuid.uuid4(),
        channel_id=None,
        chatwoot_account_id=1,
        chatwoot_conversation_id=2,
        sender_email="alice@example.com",
        body="hello",
    )


def _stub_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("polaris_api.db.pools.get_service_pool", lambda: object())
    monkeypatch.setattr("polaris_api.redis_pool.get_redis", lambda: object())
    monkeypatch.setattr(
        "polaris_api.services.chatwoot.client.get_chatwoot_client", lambda: object()
    )


async def _run_one(monkeypatch: pytest.MonkeyPatch) -> asyncio.Task[None]:
    """Start the worker, push one job, wait for it to be processed, return the
    still-running task for the caller to cancel."""
    _stub_singletons(monkeypatch)
    task = asyncio.create_task(email_assist_worker.run_email_assist_worker())
    await enqueue(_job())
    await get_queue().join()
    return task


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_drains_and_calls_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_queue_for_tests()
    calls: list[EmailAssistJob] = []

    async def _fake_generate(
        job: EmailAssistJob, *, pool: object, redis: object, chatwoot: object
    ) -> str:
        calls.append(job)
        return "posted"

    monkeypatch.setattr(email_assist, "generate_and_post_draft", _fake_generate)
    task = await _run_one(monkeypatch)
    await _stop(task)

    assert len(calls) == 1


async def test_worker_survives_handler_error(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_queue_for_tests()
    seen: list[str] = []

    async def _boom(
        job: EmailAssistJob, *, pool: object, redis: object, chatwoot: object
    ) -> str:
        seen.append("called")
        raise RuntimeError("chatwoot down")

    monkeypatch.setattr(email_assist, "generate_and_post_draft", _boom)
    task = await _run_one(monkeypatch)
    await _stop(task)

    # The error was swallowed (task_done still called → join returned) and the
    # worker is still alive until we cancel it.
    assert seen == ["called"]
