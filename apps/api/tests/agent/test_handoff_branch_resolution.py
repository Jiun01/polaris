"""WS3 — agent-mode handoff branch resolution + context threading.

Two fixes from the 2026-07-06 live verification:

  1. Fix 1 (SQL): ``_LOAD_CTX_SQL`` now resolves the branch as the
     conversation's own ``tenant_branch_id`` OR, when NULL, the channel's
     *current* branch (matched by ``(tenant_id, channel)`` kind). This keeps
     handoff honoring the branch override for conversations created before the
     channel was assigned a branch. The behavioural proof runs against a real
     DB (E2E suite / live) — the unit suite here is fake-conn only, so we pin
     the query *structure* so an accidental revert of the channel-fallback
     fails CI.

  2. Fix 2 (threading): the agent-mode ``handoff_to_human`` tool now threads
     ``customer_name`` + ``last_reply`` through ``ChatwootAdapter.handoff`` into
     ``initiate_handoff`` so ``{{customer_name}}`` / ``{{last_reply}}``
     placeholders in a branch/tenant handoff message render (parity with
     classic-mode handoff, which already passed them).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from polaris_api.services.agent.ports import ChatwootAdapter, Ports
from polaris_api.services.agent.tools.base import ToolContext
from polaris_api.services.agent.tools.handoff_to_human import handoff_to_human

_asyncio = pytest.mark.asyncio(loop_scope="function")

_TENANT = uuid4()


# ---------------------------------------------------------------------------
# Fix 1 — SQL structure guard (behavioural proof lives in the real-DB E2E suite)
# ---------------------------------------------------------------------------


def test_load_ctx_sql_falls_back_to_channel_branch() -> None:
    """The context query must resolve the branch from the channel when the
    conversation's own ``tenant_branch_id`` is NULL, matched by channel *kind*
    (old rows have NULL ``channel_id``), and still expose the same
    ``branch_after_hours`` alias downstream reads."""
    from polaris_api.services.chatwoot.handoff import _LOAD_CTX_SQL

    normalised = " ".join(_LOAD_CTX_SQL.split())
    # The load-bearing structure: conversation branch preferred, channel branch
    # the fallback, matched by (tenant_id, channel) kind — NOT channel_id (old
    # rows have NULL channel_id). Only assert the logic, not aliases/formatting.
    assert "COALESCE(" in normalised
    assert "tenant_branch_id FROM tenant_channels tc" in normalised
    assert "tc.tenant_id = c.tenant_id AND tc.channel = c.channel" in normalised
    assert "tb.id = conv.resolved_branch_id" in normalised


# ---------------------------------------------------------------------------
# Fix 2 — customer_name / last_reply threading
# ---------------------------------------------------------------------------


class _CapturingChatwootPort:
    """Records the kwargs the tool hands the port."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def handoff(
        self,
        *,
        conversation_id: Any,
        team_id: str,
        private_note: str,
        tenant_id: Any,
        customer_name: str | None = None,
        last_reply: str | None = None,
    ) -> bool:
        self.captured = {
            "conversation_id": conversation_id,
            "team_id": team_id,
            "private_note": private_note,
            "tenant_id": tenant_id,
            "customer_name": customer_name,
            "last_reply": last_reply,
        }
        return True


def _ctx(port: _CapturingChatwootPort, *, customer_name: str | None) -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        conversation_id=uuid4(),
        channel="telegram",
        channel_user_id="tg-1",
        customer_name=customer_name,
        last_user_message="please get me a person",
        conn=None,
        ports=Ports(chatwoot=port),
    )


@_asyncio
async def test_tool_threads_customer_name_and_last_reply() -> None:
    port = _CapturingChatwootPort()
    res = await handoff_to_human({"reason": "wants a human"}, _ctx(port, customer_name="Nurul"))
    assert not res.is_error
    assert port.captured["customer_name"] == "Nurul"
    assert port.captured["last_reply"] == "please get me a person"


@_asyncio
async def test_tool_threads_none_customer_name_without_error() -> None:
    # Telegram senders can lack a name — must pass None through cleanly.
    port = _CapturingChatwootPort()
    await handoff_to_human({"reason": "x"}, _ctx(port, customer_name=None))
    assert port.captured["customer_name"] is None
    assert port.captured["last_reply"] == "please get me a person"


@_asyncio
async def test_adapter_forwards_context_to_initiate_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ChatwootAdapter.handoff must forward customer_name/last_reply to
    initiate_handoff (regression: they were hardcoded None)."""
    captured: dict[str, Any] = {}

    async def _fake_initiate(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(outcome="confirmed")

    # initiate_handoff is imported inside the method from its source module.
    monkeypatch.setattr(
        "polaris_api.services.chatwoot.handoff.initiate_handoff", _fake_initiate
    )

    adapter = ChatwootAdapter(
        conn=MagicMock(), chatwoot_client=MagicMock(), meta_client=MagicMock()
    )
    ok = await adapter.handoff(
        conversation_id=uuid4(),
        team_id="0",
        private_note="note",
        tenant_id=_TENANT,
        customer_name="Nurul",
        last_reply="help me",
    )
    assert ok is True
    assert captured["customer_name"] == "Nurul"
    assert captured["last_reply"] == "help me"
    # intent_tier stays None — agent runtime has no intent tier concept.
    assert captured["intent_tier"] is None
