"""Capture-service unit tests — Knowledge Gap Detection / M1.

Covers ``services/knowledge_gaps/capture.py``:
  * ``classify_retrieval_miss`` — the pure miss-decision helper (no DB).
  * ``record_miss`` — the fail-soft INSERT (fake connection; no DB / network).

No real Postgres here — ``classify_retrieval_miss`` is pure, and ``record_miss``
is exercised against a fake connection so we can assert both the happy-path
INSERT and the critical fail-soft guarantee (a capture error must never raise).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from polaris_api.services.knowledge_gaps import capture
from polaris_shared_types.knowledge_chunk import KnowledgeChunkHit


def _hit(rrf_score: float) -> KnowledgeChunkHit:
    return KnowledgeChunkHit(
        id=uuid4(),
        content="…",
        source_name="doc",
        rrf_score=rrf_score,
    )


# ---- classify_retrieval_miss (pure) ----


def test_no_chunks_is_no_retrieval() -> None:
    is_miss, signal, score = capture.classify_retrieval_miss([], threshold=0.0)
    assert is_miss is True
    assert signal == "no_retrieval"
    assert score is None


def test_healthy_retrieval_is_not_a_miss() -> None:
    is_miss, signal, score = capture.classify_retrieval_miss(
        [_hit(0.8)], threshold=0.5
    )
    assert is_miss is False
    assert signal is None
    assert score == 0.8


def test_low_score_below_threshold_is_low_retrieval() -> None:
    is_miss, signal, score = capture.classify_retrieval_miss(
        [_hit(0.2)], threshold=0.5
    )
    assert is_miss is True
    assert signal == "low_retrieval"
    assert score == 0.2


def test_threshold_zero_disables_low_retrieval() -> None:
    # GA default: threshold 0.0 → low_retrieval never fires, even on a weak top
    # score. The chunk's score is still reported back for telemetry.
    is_miss, signal, score = capture.classify_retrieval_miss(
        [_hit(0.01)], threshold=0.0
    )
    assert is_miss is False
    assert signal is None
    assert score == 0.01


def test_uses_max_rrf_score_regardless_of_order() -> None:
    # Defensive: don't assume chunks[0] is the best; use the max.
    is_miss, signal, score = capture.classify_retrieval_miss(
        [_hit(0.1), _hit(0.9), _hit(0.4)], threshold=0.5
    )
    assert is_miss is False
    assert score == 0.9


def test_score_exactly_at_threshold_is_not_a_miss() -> None:
    # Strictly-below semantics: a score equal to the threshold passes.
    is_miss, _signal, _score = capture.classify_retrieval_miss(
        [_hit(0.5)], threshold=0.5
    )
    assert is_miss is False


# ---- classify_score_miss (shared core; flow path) ----


def test_score_none_is_no_retrieval() -> None:
    is_miss, signal, score = capture.classify_score_miss(None, threshold=0.0)
    assert is_miss is True
    assert signal == "no_retrieval"
    assert score is None


def test_score_below_threshold_is_low_retrieval() -> None:
    is_miss, signal, score = capture.classify_score_miss(0.2, threshold=0.5)
    assert (is_miss, signal, score) == (True, "low_retrieval", 0.2)


def test_score_above_threshold_is_not_a_miss() -> None:
    is_miss, signal, score = capture.classify_score_miss(0.8, threshold=0.5)
    assert (is_miss, signal, score) == (False, None, 0.8)


def test_score_threshold_zero_disables_low_retrieval() -> None:
    is_miss, signal, _ = capture.classify_score_miss(0.01, threshold=0.0)
    assert (is_miss, signal) == (False, None)


# ---- parse_stashed_score (flow-path fail-soft parse) ----


def test_parse_none_is_none() -> None:
    assert capture.parse_stashed_score(None) is None


def test_parse_valid_float_string() -> None:
    assert capture.parse_stashed_score("0.42") == 0.42


def test_parse_valid_float_object() -> None:
    assert capture.parse_stashed_score(0.42) == 0.42


def test_parse_garbage_is_none_not_raise() -> None:
    # A malformed value must yield None, never raise — it runs outside the
    # record_miss savepoint and could otherwise drop the customer reply.
    assert capture.parse_stashed_score("not-a-number") is None
    assert capture.parse_stashed_score({"x": 1}) is None


def test_parse_non_finite_is_none() -> None:
    # nan/inf parse cleanly via float() but must not reach the DB / comparisons.
    assert capture.parse_stashed_score("nan") is None
    assert capture.parse_stashed_score("inf") is None
    assert capture.parse_stashed_score("-inf") is None


# ---- record_miss (fail-soft INSERT) ----


class _FakeConn:
    """Minimal asyncpg-Connection stand-in for record_miss.

    Records execute() calls and supports the nested-``transaction()`` savepoint
    record_miss wraps its INSERT in. ``raise_on_execute`` simulates a DB error
    so we can prove capture is fail-soft.
    """

    def __init__(self, *, raise_on_execute: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._raise = raise_on_execute

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        if self._raise:
            raise RuntimeError("simulated DB failure")
        return "INSERT 0 1"


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False  # don't suppress — mirror asyncpg savepoint rollback path


@pytest.mark.asyncio
async def test_record_miss_inserts_row() -> None:
    conn = _FakeConn()
    tenant_id = uuid4()
    conversation_id = uuid4()
    message_id = uuid4()

    await capture.record_miss(
        conn,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        customer_question="do you ship internationally?",
        signal_type="no_retrieval",
        retrieval_top_score=None,
        channel="whatsapp",
        detected_language="en",
    )

    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "INSERT INTO knowledge_gap_misses" in sql
    assert tenant_id in args
    assert conversation_id in args
    assert message_id in args
    assert "do you ship internationally?" in args
    assert "no_retrieval" in args
    assert "whatsapp" in args


@pytest.mark.asyncio
async def test_record_miss_truncates_long_question() -> None:
    conn = _FakeConn()
    await capture.record_miss(
        conn,  # type: ignore[arg-type]
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        message_id=None,
        customer_question="x" * 5000,
        signal_type="no_retrieval",
        retrieval_top_score=None,
        channel="whatsapp",
        detected_language=None,
    )
    _sql, args = conn.calls[0]
    stored = next(a for a in args if isinstance(a, str) and set(a) == {"x"})
    assert len(stored) == capture._MAX_QUESTION_LEN


@pytest.mark.asyncio
async def test_record_miss_is_fail_soft() -> None:
    # A DB error inside capture must NOT propagate — the customer reply has
    # already been persisted/dispatched; losing a gap row is acceptable, losing
    # the reply is not.
    conn = _FakeConn(raise_on_execute=True)

    # Must not raise.
    await capture.record_miss(
        conn,  # type: ignore[arg-type]
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        message_id=None,
        customer_question="anything",
        signal_type="handoff",
        retrieval_top_score=None,
        channel="telegram",
        detected_language=None,
    )

    assert len(conn.calls) == 1  # it tried, then swallowed the error


# ---- tenant_in_capture_tier (Ultra gate) ----


class _TierConn:
    def __init__(
        self, plan_tier: str | None = None, *, raise_on_fetch: bool = False
    ) -> None:
        self._plan_tier = plan_tier
        self._raise = raise_on_fetch

    async def fetchrow(self, _sql: str, *_args: object) -> dict[str, str] | None:
        if self._raise:
            raise RuntimeError("simulated DB failure")
        return None if self._plan_tier is None else {"plan_tier": self._plan_tier}


_ULTRA_TIERS = frozenset({"ultra", "enterprise"})


@pytest.mark.asyncio
async def test_tier_ultra_is_captured() -> None:
    conn = _TierConn("ultra")
    assert await capture.tenant_in_capture_tier(conn, uuid4(), _ULTRA_TIERS) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tier_starter_is_not_captured() -> None:
    conn = _TierConn("starter")
    assert await capture.tenant_in_capture_tier(conn, uuid4(), _ULTRA_TIERS) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tier_missing_tenant_row_is_not_captured() -> None:
    conn = _TierConn(None)
    assert await capture.tenant_in_capture_tier(conn, uuid4(), _ULTRA_TIERS) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tier_read_error_defaults_closed() -> None:
    conn = _TierConn(raise_on_fetch=True)
    assert await capture.tenant_in_capture_tier(conn, uuid4(), _ULTRA_TIERS) is False  # type: ignore[arg-type]


# ---- echo-drop invariant regression (#2 cleanup) ----
#
# The chatwoot_router.py:505 echo-drop invariant ensures outgoing + agent_bot
# messages NEVER reach the pipeline enqueue and therefore NEVER reach record_miss.
# This test pins ``human_agent_forward_inbox_id`` so the invariant can't be
# silently removed: bot echoes must return None (not a forward-worthy event).


def test_bot_echo_is_not_a_human_agent_forward() -> None:
    """outgoing + sender.type='agent_bot' must return None from the echo-drop gate.

    This is the router-level invariant.  A None return means the webhook handler
    falls straight through to 200 OK without enqueuing the pipeline, so
    record_miss is never called.  Bot greeting echoes that leaked into
    knowledge_gap_misses on 2026-06-10 happened because this invariant was not
    yet enforced during the chatwoot-native cutover.
    """
    from polaris_api.webhooks.chatwoot_router import human_agent_forward_inbox_id

    bot_echo_payload: dict[str, object] = {
        "message_type": "outgoing",
        "sender": {"type": "agent_bot", "id": 1},
        "conversation": {"id": 42, "inbox_id": 7, "status": "pending"},
        "content": "Hey there! Welcome to Polaris support.",
    }
    result = human_agent_forward_inbox_id(bot_echo_payload)
    assert result is None, (
        "Echo-drop invariant broken: human_agent_forward_inbox_id returned "
        f"{result!r} for an outgoing agent_bot payload — bot echoes would reach "
        "the pipeline and be captured as knowledge gaps."
    )


def test_human_agent_reply_is_forwarded() -> None:
    """outgoing + sender.type='agent' + inbox.id SHOULD return an inbox id (not an echo).

    Counter-case: a real human-agent reply must still be forwarded.  This keeps
    the assertion above honest — we're testing the discriminating condition, not
    just that the function always returns None.

    Note: human_agent_forward_inbox_id reads ``payload["inbox"]["id"]``, not
    ``conversation.inbox_id`` — mirrors the actual Chatwoot webhook envelope shape.
    """
    from polaris_api.webhooks.chatwoot_router import human_agent_forward_inbox_id

    human_reply_payload: dict[str, object] = {
        "message_type": "outgoing",
        "sender": {"type": "agent", "id": 99},
        "inbox": {"id": 7},
        "conversation": {"id": 42, "status": "open"},
        "content": "Hi, I'll look into your refund right away.",
    }
    result = human_agent_forward_inbox_id(human_reply_payload)
    # A non-None inbox id means the handler will forward (not echo-drop).
    assert result is not None, (
        "human_agent_forward_inbox_id returned None for a genuine human-agent "
        "reply — human-agent messages would be silently dropped."
    )


def test_incoming_message_is_not_a_human_agent_forward() -> None:
    """incoming messages return None — they go through the main pipeline path."""
    from polaris_api.webhooks.chatwoot_router import human_agent_forward_inbox_id

    incoming_payload: dict[str, object] = {
        "message_type": "incoming",
        "sender": {"type": "contact", "id": 55},
        "conversation": {"id": 10, "inbox_id": 3, "status": "pending"},
        "content": "I need help with my order.",
    }
    result = human_agent_forward_inbox_id(incoming_payload)
    assert result is None
