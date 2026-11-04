"""Tests for the demo-aware pipeline entry point — ADR-066.

Validates `pipeline.run_inbound` branching (default / continue / activate /
expire / refuse) by stubbing demo_runtime + the session/lead/dispatch helpers,
and the `_resolve_channel_owner` dispatch seam. No live DB needed — the heavy
end-to-end (real KB + dispatch) is covered by the smoke/E2E harness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from polaris_api.services import demo_lead, demo_runtime, demo_sessions, pipeline
from polaris_api.services.demo_runtime import DemoDecision
from polaris_shared_types import (
    IncomingMessage,
    PipelineDecision,
    ResponseEnvelope,
    SenderRef,
    UsageMetrics,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")

_GATEWAY = demo_sessions.POLARIS_INTERNAL_TENANT_ID
_BRAND = UUID("11111111-1111-1111-1111-111111111111")


def _incoming() -> IncomingMessage:
    return IncomingMessage(
        tenant_id=_GATEWAY,
        channel_id=uuid4(),
        provider="whatsapp",
        provider_message_id="wamid.test",
        sender=SenderRef(channel_user_id="+60123", display_name="Jane"),
        body="hi",
        received_at=datetime.now(UTC),
        chatwoot_account_id=5,
        chatwoot_conversation_id=42,
    )


def _deps() -> Any:
    return pipeline.PipelineDeps(
        anon_pool=AsyncMock(), redis=AsyncMock(), meta_client=AsyncMock()
    )


def _decision() -> PipelineDecision:
    return PipelineDecision(
        conversation_id=uuid4(),
        tenant_id=_BRAND,
        user_message_id=uuid4(),
        assistant_message_id=uuid4(),
        response=ResponseEnvelope(
            content="ok",
            intent="simple",
            model_used="haiku",
            language_detected="en",
            usage=UsageMetrics(input_tokens=1, output_tokens=1),
            latency_ms=1,
        ),
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    run = AsyncMock(return_value=_decision())
    dispatch = AsyncMock(return_value="wamid.sent")
    activate = AsyncMock()
    expire = AsyncMock()
    increment = AsyncMock()
    flag = AsyncMock()
    monkeypatch.setattr(pipeline, "run", run)
    monkeypatch.setattr(pipeline, "_dispatch_reply", dispatch)
    monkeypatch.setattr(demo_sessions, "activate_demo_session", activate)
    monkeypatch.setattr(demo_sessions, "expire_demo_session", expire)
    monkeypatch.setattr(demo_sessions, "increment_demo_message", increment)
    monkeypatch.setattr(demo_lead, "flag", flag)
    return {
        "run": run,
        "dispatch": dispatch,
        "activate": activate,
        "expire": expire,
        "increment": increment,
        "flag": flag,
    }


def _active() -> demo_sessions.ActiveDemoSession:
    return demo_sessions.ActiveDemoSession(
        session_id=uuid4(),
        tenant_id=_BRAND,
        demo_code="trgwatches1824",
        message_count=3,
        max_messages=50,
        expires_at=datetime.now(UTC) + timedelta(hours=5),
        chatwoot_conversation_id=42,
        chatwoot_inbox_id=7,
    )


_DEMO = demo_sessions.DemoTenant(
    tenant_id=_BRAND,
    slug="trg-watch",
    display_name="TRG Watch",
    demo_code="trgwatches1824",
    duration_hours=6,
    max_messages=50,
)


async def test_default_runs_pipeline_without_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _patch_common(monkeypatch)
    monkeypatch.setattr(
        demo_runtime,
        "resolve",
        AsyncMock(
            return_value=DemoDecision(
                mode="default", content_tenant_id=_GATEWAY, brand_demo=False
            )
        ),
    )
    await pipeline.run_inbound(incoming=_incoming(), deps=_deps())

    m["run"].assert_awaited_once()
    assert m["run"].call_args.kwargs["demo"] is False
    assert m["run"].call_args.kwargs["incoming"].tenant_id == _GATEWAY
    m["dispatch"].assert_not_called()


async def test_run_inbound_uses_channel_owner_as_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-066 regression: the gateway/default tenant is the CHANNEL OWNER
    (incoming.tenant_id), not a hardcoded polaris-internal — a no-code message
    must be answered by the operator's own tenant, never rerouted."""
    m = _patch_common(monkeypatch)
    operator = UUID("33333333-3333-3333-3333-333333333333")  # the operator's real tenant
    resolve = AsyncMock(
        return_value=DemoDecision(
            mode="default", content_tenant_id=operator, brand_demo=False
        )
    )
    monkeypatch.setattr(demo_runtime, "resolve", resolve)

    inc = _incoming().model_copy(update={"tenant_id": operator})
    await pipeline.run_inbound(incoming=inc, deps=_deps())

    # run_inbound passed the channel owner as the gateway (not polaris-internal)
    assert resolve.call_args.kwargs["gateway_tenant_id"] == operator
    # default → pipeline runs under the operator tenant, not rerouted
    m["run"].assert_awaited_once()
    assert m["run"].call_args.kwargs["incoming"].tenant_id == operator


async def test_continue_runs_pipeline_demo_and_increments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _patch_common(monkeypatch)
    sess = _active()
    monkeypatch.setattr(
        demo_runtime,
        "resolve",
        AsyncMock(
            return_value=DemoDecision(
                mode="continue_demo",
                content_tenant_id=_BRAND,
                brand_demo=True,
                session=sess,
            )
        ),
    )
    await pipeline.run_inbound(incoming=_incoming(), deps=_deps())

    m["run"].assert_awaited_once()
    assert m["run"].call_args.kwargs["demo"] is True
    # content tenant rewritten to the brand for RAG/persona scoping
    assert m["run"].call_args.kwargs["incoming"].tenant_id == _BRAND
    m["increment"].assert_awaited_once_with(sess.session_id)


async def test_activate_creates_session_and_sends_welcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _patch_common(monkeypatch)
    monkeypatch.setattr(
        demo_runtime,
        "resolve",
        AsyncMock(
            return_value=DemoDecision(
                mode="activate",
                content_tenant_id=_BRAND,
                brand_demo=True,
                direct_message="Welcome to the TRG Watch demo!",
                demo=_DEMO,
            )
        ),
    )
    await pipeline.run_inbound(incoming=_incoming(), deps=_deps())

    m["activate"].assert_awaited_once()
    m["dispatch"].assert_awaited_once()
    # canned welcome dispatched on the GATEWAY tenant (owns the channel)
    assert m["dispatch"].call_args.kwargs["tenant_id"] == _GATEWAY
    m["run"].assert_not_called()


async def test_expire_expires_flags_and_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _patch_common(monkeypatch)
    sess = _active()
    monkeypatch.setattr(
        demo_runtime,
        "resolve",
        AsyncMock(
            return_value=DemoDecision(
                mode="expire",
                content_tenant_id=_GATEWAY,
                brand_demo=False,
                direct_message="Your TRG Watch demo has ended.",
                session=sess,
            )
        ),
    )
    # get_chatwoot_client is imported locally inside run_inbound
    from polaris_api.services.chatwoot import client as cw_client_mod

    monkeypatch.setattr(cw_client_mod, "get_chatwoot_client", lambda: AsyncMock())

    await pipeline.run_inbound(incoming=_incoming(), deps=_deps())

    m["expire"].assert_awaited_once_with(sess.session_id)
    m["flag"].assert_awaited_once()
    m["dispatch"].assert_awaited_once()
    m["run"].assert_not_called()


async def test_refuse_sends_canned_no_session_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _patch_common(monkeypatch)
    monkeypatch.setattr(
        demo_runtime,
        "resolve",
        AsyncMock(
            return_value=DemoDecision(
                mode="refuse",
                content_tenant_id=_GATEWAY,
                brand_demo=False,
                direct_message="You've already used your demo.",
                demo=_DEMO,
            )
        ),
    )
    await pipeline.run_inbound(incoming=_incoming(), deps=_deps())

    m["dispatch"].assert_awaited_once()
    m["activate"].assert_not_called()
    m["expire"].assert_not_called()
    m["run"].assert_not_called()


# --- channel-owner dispatch seam --------------------------------------------

async def test_resolve_channel_owner_returns_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = _GATEWAY

    class _Acq:
        async def __aenter__(self) -> Any:
            return conn

        async def __aexit__(self, *_: Any) -> None:
            return None

    class _Pool:
        def acquire(self) -> _Acq:
            return _Acq()

    import polaris_api.db.pools as pools

    monkeypatch.setattr(pools, "get_service_pool", lambda: _Pool())
    cid = uuid4()
    assert await pipeline._resolve_channel_owner(cid) == _GATEWAY


async def test_resolve_channel_owner_none_when_no_channel_id() -> None:
    assert await pipeline._resolve_channel_owner(None) is None
