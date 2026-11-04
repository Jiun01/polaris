"""Pipeline STOP keyword + quota enforcement tests — Phase 10 PR-1.

Tests the two new early-exit paths added to pipeline.run():
  (a) STOP keyword → PipelineDecision(stop_blocked=True)
  (b) Quota exceeded → PipelineDecision(quota_exceeded=True)

Uses the same _FakeConn + AsyncMock Redis approach as other pipeline tests.
pipeline.run() is called directly with a PipelineDeps stub so no FastAPI
app is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from polaris_shared_types import IncomingMessage, SenderRef
from polaris_shared_types.llm_response import ResponseEnvelope, UsageMetrics

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ── minimal fakes ─────────────────────────────────────────────────────────────


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._d


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakeConn:
    """Minimal asyncpg.Connection stand-in."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._row_map: dict[str, dict[str, Any] | None] = {}
        self._val_map: dict[str, Any] = {}

    def canned_row(self, frag: str, row: dict[str, Any] | None) -> None:
        self._row_map[frag] = row

    def canned_val(self, frag: str, val: Any) -> None:
        self._val_map[frag] = val

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRecord | None:
        for frag, row in self._row_map.items():
            if frag in sql:
                return _FakeRecord(row) if row is not None else None
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        for frag, val in self._val_map.items():
            if frag in sql:
                return val
        return None

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
        return []


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


def _make_incoming(
    tenant_id: UUID,
    body: str,
    channel_user_id: str = "user-001",
) -> IncomingMessage:
    return IncomingMessage(
        tenant_id=tenant_id,
        channel_id=uuid4(),
        provider="whatsapp",
        provider_message_id="test-wamid-001",
        body=body,
        sender=SenderRef(channel_user_id=channel_user_id),
        received_at=datetime.now(UTC),
    )


def _make_deps(conn: _FakeConn, redis: Any) -> Any:
    from polaris_api.services.meta_whatsapp import MetaWhatsAppClient
    from polaris_api.services.pipeline import PipelineDeps
    pool = _FakePool(conn)
    meta = MagicMock(spec=MetaWhatsAppClient)
    return PipelineDeps(anon_pool=pool, redis=redis, meta_client=meta)  # type: ignore[arg-type]


def _make_redis(current_count: int = 0, cap: int = 5000) -> Any:
    """Redis mock that returns ``current_count`` from GET."""
    r = AsyncMock()
    r.get.return_value = str(current_count).encode() if current_count > 0 else None
    r.incr.return_value = current_count + 1
    r.expire.return_value = True
    r.scan.return_value = (0, [])
    return r


def _seed_conn_for_normal_pipeline(
    conn: _FakeConn,
    tenant_id: UUID,
    cap: int = 5000,
) -> None:
    """Seed a FakeConn so the conversation-setup phase succeeds."""
    conv_id = uuid4()
    msg_id = uuid4()
    # _ensure_conversation INSERT → row
    conn.canned_row("INSERT INTO conversations", {"id": conv_id, "tenant_branch_id": None})
    # save_message
    conn.canned_val("INSERT INTO messages", msg_id)
    # quota check → bot_replies_period_cap
    conn.canned_row(
        "bot_replies_period_cap",
        {"bot_replies_period_cap": cap, "plan_tier": "starter"},
    )
    # chatwoot recovery hook → no pending recovery
    conn.canned_val("chatwoot_recovery", None)


# ── STOP keyword tests ────────────────────────────────────────────────────────


async def test_stop_keyword_returns_stop_blocked_decision() -> None:
    """STOP BOT REPLY → PipelineDecision(stop_blocked=True), no dispatch."""
    tenant_id = uuid4()
    conn = _FakeConn()
    conn.canned_row("INSERT INTO conversations", {"id": uuid4(), "tenant_branch_id": None})

    redis = _make_redis(current_count=0)
    deps = _make_deps(conn, redis)

    # Patch out the chatwoot recovery hook (irrelevant here).
    with (
        patch(
            "polaris_api.services.chatwoot.recovery.check_pending_recovery",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "polaris_api.services.conversation.save_message",
            new=AsyncMock(return_value=uuid4()),
        ),
        patch(
            "polaris_api.services.pipeline.mark_opted_out",
            new=AsyncMock(return_value=True),
        ) as mock_mark,
    ):
        from polaris_api.services import pipeline
        decision = await pipeline.run(
            incoming=_make_incoming(tenant_id, "stop bot reply"),
            deps=deps,
        )

    assert decision.stop_blocked is True
    assert decision.quota_exceeded is False
    assert decision.handoff_emitted is False
    assert decision.response is None
    assert decision.assistant_message_id is None
    assert decision.provider_message_id is None
    mock_mark.assert_awaited_once()


# ── M1: human-handoff suppression ─────────────────────────────────────────────


async def test_handoff_at_set_suppresses_bot_reply() -> None:
    """A conversation already handed to a human (``handoff_at`` set) → the next
    inbound returns PipelineDecision(handoff_suppressed=True) with no reply and
    no dispatch (QA finding M1). The customer message is still persisted."""
    tenant_id = uuid4()
    conn = _FakeConn()
    conn.canned_row("INSERT INTO conversations", {"id": uuid4(), "tenant_branch_id": None})
    # The conversation was handed to a human earlier — handoff_at is set and
    # not yet cleared by a reverse-handoff.
    conn.canned_val("SELECT handoff_at", datetime.now(UTC))

    redis = _make_redis(current_count=0)
    deps = _make_deps(conn, redis)

    with (
        patch(
            "polaris_api.services.chatwoot.recovery.check_pending_recovery",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "polaris_api.services.conversation.save_message",
            new=AsyncMock(return_value=uuid4()),
        ),
    ):
        from polaris_api.services import pipeline
        decision = await pipeline.run(
            incoming=_make_incoming(tenant_id, "are you there?"),
            deps=deps,
        )

    assert decision.handoff_suppressed is True
    assert decision.response is None
    assert decision.assistant_message_id is None
    assert decision.provider_message_id is None
    assert decision.stop_blocked is False
    assert decision.quota_exceeded is False


async def test_stop_keyword_bm_berhenti() -> None:
    """BERHENTI (BM) also triggers stop_blocked."""
    tenant_id = uuid4()
    conn = _FakeConn()
    conn.canned_row("INSERT INTO conversations", {"id": uuid4(), "tenant_branch_id": None})
    redis = _make_redis()
    deps = _make_deps(conn, redis)

    with (
        patch(
            "polaris_api.services.chatwoot.recovery.check_pending_recovery",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "polaris_api.services.conversation.save_message",
            new=AsyncMock(return_value=uuid4()),
        ),
        patch(
            "polaris_api.services.pipeline.mark_opted_out",
            new=AsyncMock(return_value=True),
        ),
    ):
        from polaris_api.services import pipeline
        decision = await pipeline.run(
            incoming=_make_incoming(tenant_id, "BERHENTI"),
            deps=deps,
        )

    assert decision.stop_blocked is True


async def test_non_stop_text_does_not_trigger_stop_blocked() -> None:
    """A normal message must not be treated as STOP."""
    tenant_id = uuid4()
    conn = _FakeConn()
    conn.canned_row("INSERT INTO conversations", {"id": uuid4(), "tenant_branch_id": None})
    # _persist_assistant_message calls conn.fetchrow("INSERT INTO messages …")
    # directly (not via conversation.save_message).  Seed _row_map so it
    # doesn't raise "no row returned".  Include created_at: the function
    # reads both row["id"] and row["created_at"].
    conn.canned_row("INSERT INTO messages", {"id": uuid4(), "created_at": datetime.now(UTC)})
    conn.canned_row(
        "bot_replies_period_cap",
        {"bot_replies_period_cap": 5000, "plan_tier": "starter"},
    )

    redis = _make_redis(current_count=0)
    deps = _make_deps(conn, redis)

    # Patch the heavy LLM calls so the test stays fast.
    with (
        patch(
            "polaris_api.services.chatwoot.recovery.check_pending_recovery",
            new=AsyncMock(return_value=None),
        ),
        patch("polaris_api.services.llm.classify_intent", new=AsyncMock(return_value="simple")),
        patch("polaris_api.services.embed.embed_query", new=AsyncMock(return_value=[])),
        patch("polaris_api.services.rag.search_hybrid", new=AsyncMock(return_value=[])),
        patch(
            "polaris_api.services.conversation.load_history",
            new=AsyncMock(return_value=MagicMock(messages=[])),
        ),
        patch(
            "polaris_api.services.tenant_snapshot.load_tenant_snapshot",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "polaris_api.services.llm.generate_response",
            new=AsyncMock(
                return_value=ResponseEnvelope(
                    content="Hi!",
                    intent="simple",
                    model_used="haiku",
                    language_detected="en",
                    latency_ms=42,
                    usage=UsageMetrics(
                        input_tokens=10,
                        output_tokens=5,
                        cache_read_tokens=0,
                        cache_creation_tokens=0,
                    ),
                )
            ),
        ),
        patch("polaris_api.services.conversation.save_message", new=AsyncMock(return_value=uuid4())),
        patch("polaris_api.services.conversation.append_assistant_to_cache", new=AsyncMock()),
        patch("polaris_api.services.reply_dispatch.send_reply", new=AsyncMock(return_value="wamid-001")),
    ):
        from polaris_api.services import pipeline
        decision = await pipeline.run(
            incoming=_make_incoming(tenant_id, "What are your opening hours?"),
            deps=deps,
        )

    assert decision.stop_blocked is False


# ── quota exceeded tests ──────────────────────────────────────────────────────


async def test_quota_exceeded_returns_quota_exceeded_decision() -> None:
    """When Redis counter ≥ cap, pipeline exits with quota_exceeded=True."""
    tenant_id = uuid4()
    conn = _FakeConn()
    conn.canned_row("INSERT INTO conversations", {"id": uuid4(), "tenant_branch_id": None})
    conn.canned_val("INSERT INTO messages", uuid4())
    conn.canned_row(
        "bot_replies_period_cap",
        {"bot_replies_period_cap": 5000, "plan_tier": "starter"},
    )

    # Counter already at cap.
    redis = _make_redis(current_count=5000)
    deps = _make_deps(conn, redis)

    with (
        patch(
            "polaris_api.services.chatwoot.recovery.check_pending_recovery",
            new=AsyncMock(return_value=None),
        ),
        patch("polaris_api.services.conversation.save_message", new=AsyncMock(return_value=uuid4())),
        patch(
            "polaris_api.services.pipeline._escalate_quota_exhausted",
            new=AsyncMock(),
        ) as escalate,
    ):
        from polaris_api.services import pipeline
        decision = await pipeline.run(
            incoming=_make_incoming(tenant_id, "Hello there"),
            deps=deps,
        )

    assert decision.quota_exceeded is True
    assert decision.stop_blocked is False
    assert decision.response is None
    assert decision.assistant_message_id is None
    # Step 5: over-cap now escalates (holding message + CW hand-off) instead of
    # dropping the reply silently — verify the call shape, not just that it ran.
    escalate.assert_awaited_once_with(
        conn=ANY,
        deps=ANY,
        incoming=ANY,
        channel="whatsapp",
        cw_account_id=ANY,
        cw_conv_id=ANY,
    )
    # No LLM calls made — quota check is before classify.
