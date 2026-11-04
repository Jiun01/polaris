"""Regression: flow & agent reply paths must count against bot-reply quota.

Audit finding API-07/08. ``_run_inner`` used to return early when the agent
or flow runtime produced a reply (``return agent_decision`` /
``return flow_decision``) — *before* reaching the classic-path
``increment_counter`` call. The fix routed all reply paths through the shared
``_increment_bot_reply_quota`` helper. Without it, any tenant in agent mode
(formerly: on a published flow too, before audit Batch D retired
flow_runtime) sends LLM replies that never advance the usage counter, so
``over_cap`` never fires (billing undercount / unlimited free replies).

Two layers of protection:
  * Contract — the helper increments iff a reply was dispatched and it's not a
    brand-demo turn; a Redis hiccup is swallowed (never rolls back the reply).
  * Wiring — the helper is actually invoked at *both* surviving call sites
    (classic + agent; the flow call site was retired along with flow_runtime
    in audit Batch D). A future edit that re-removes the agent call would
    resurrect the bug; the source-inspection test below fails loudly if a
    call site disappears.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services import pipeline

# Function-scoped loop so these async tests never share/close the session-scoped
# event loop that conftest's service_dsn holds (see conftest.py:21-37).
pytestmark = pytest.mark.asyncio(loop_scope="function")


def _deps_with_redis() -> SimpleNamespace:
    """Minimal duck-typed PipelineDeps carrying just the ``.redis`` the helper
    touches. (tests aren't in the strict-mypy scope, so SimpleNamespace is fine.)"""
    return SimpleNamespace(redis=AsyncMock())


async def test_increment_called_when_reply_dispatched_and_not_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(pipeline.quota, "increment_counter", spy)

    tenant_id = uuid4()
    deps = _deps_with_redis()
    await pipeline._increment_bot_reply_quota(
        deps, tenant_id=tenant_id, provider_message_id="wamid.123", demo=False,
    )

    spy.assert_awaited_once()
    assert spy.await_args.kwargs["tenant_id"] == tenant_id


async def test_increment_skipped_for_demo_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-066: brand-demo replies do not consume the tenant's quota."""
    spy = AsyncMock()
    monkeypatch.setattr(pipeline.quota, "increment_counter", spy)

    await pipeline._increment_bot_reply_quota(
        _deps_with_redis(), tenant_id=uuid4(), provider_message_id="wamid.1", demo=True,
    )
    spy.assert_not_called()


async def test_increment_skipped_when_no_reply_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider_message_id → nothing was sent → nothing to count."""
    spy = AsyncMock()
    monkeypatch.setattr(pipeline.quota, "increment_counter", spy)

    await pipeline._increment_bot_reply_quota(
        _deps_with_redis(), tenant_id=uuid4(), provider_message_id=None, demo=False,
    )
    spy.assert_not_called()


async def test_redis_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis hiccup on increment must not propagate — the reply is already
    committed and dispatched; the counter is reconciled by the nightly flush."""
    boom = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(pipeline.quota, "increment_counter", boom)

    # Must not raise.
    await pipeline._increment_bot_reply_quota(
        _deps_with_redis(), tenant_id=uuid4(), provider_message_id="wamid.9", demo=False,
    )
    boom.assert_awaited_once()


async def test_all_reply_paths_wire_the_quota_helper() -> None:
    """Mechanical guard: classic + agent paths must each call the shared
    increment helper. Pins the exact API-07/08 regression — a future edit
    that drops the agent call would silently re-introduce the quota
    undercount, and this test fails when that happens.

    (The flow-runtime early-return branch this test used to also pin was
    retired in audit Batch D — flow_runtime is deleted, so there is no
    third call site anymore.)
    """
    src = inspect.getsource(pipeline._run_inner)

    # Classic path + the agent early-return branch = at least two calls.
    # (The booking-command path is INTENTIONALLY exempt per ADR-066 — it returns
    # before any quota site and is not expected to increment, so the count is 2,
    # not 3.) The provider_message_id assertion below is the precise guard:
    # it pins that the early-return branch passes its own runtime's id.
    assert src.count("_increment_bot_reply_quota(") >= 2, (
        "expected the quota helper at the classic and agent call sites"
    )
    assert "agent_decision.provider_message_id" in src
