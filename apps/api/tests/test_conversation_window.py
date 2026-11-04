"""Window-trim tests — Phase 4 PR-3.

Per ADR-031: window-trim keeps at most ``max_messages`` turns OR
``max_tokens`` total tokens, whichever ceiling hits first walking
from the most-recent message backward. The ``truncated_by`` flag on
the returned ConversationWindow lets the Phase 9 dashboard chart
which ceiling actually binds in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _make_conn(rows: list[dict]) -> AsyncMock:
    """asyncpg returns DESC; fixture mirrors that ordering."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=list(reversed(rows)))
    return conn


def _msg(role: str, content: str, ts: datetime, *, tc: int | None = None) -> dict:
    return {"role": role, "content": content, "created_at": ts, "token_count": tc}


async def test_under_both_caps_returns_all_messages() -> None:
    """5 short messages, defaults 20/8k → all 5 returned, truncated='none'."""
    from polaris_api.services import conversation

    base = datetime.now(UTC)
    rows = [
        _msg("user", f"q{i}", base + timedelta(seconds=i), tc=5)
        for i in range(5)
    ]
    conn = _make_conn(rows)

    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
    )

    assert len(window.messages) == 5
    assert window.truncated_by == "none"
    # All chronological.
    assert [m.content for m in window.messages] == ["q0", "q1", "q2", "q3", "q4"]


async def test_message_count_cap_keeps_most_recent_n() -> None:
    """50 messages w/ max_messages=20 → last 20 kept, truncated='message_count'."""
    from polaris_api.services import conversation

    base = datetime.now(UTC)
    rows = [
        _msg("user", f"msg{i}", base + timedelta(seconds=i), tc=5)
        for i in range(50)
    ]
    conn = _make_conn(rows)

    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
        max_messages=20,
    )

    assert len(window.messages) == 20
    assert window.truncated_by == "message_count"
    # Most-recent 20 kept (msg30..msg49), chronological.
    assert window.messages[0].content == "msg30"
    assert window.messages[-1].content == "msg49"


async def test_token_cap_kicks_in_before_message_cap() -> None:
    """Few long messages: token cap binds first."""
    from polaris_api.services import conversation

    base = datetime.now(UTC)
    # 10 messages of 3000 tokens each = 30000 tokens. max_tokens=8000
    # should keep only the last ~2-3.
    rows = [
        _msg("user", f"long-{i}", base + timedelta(seconds=i), tc=3000)
        for i in range(10)
    ]
    conn = _make_conn(rows)

    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
        max_messages=20, max_tokens=8000,
    )

    assert window.truncated_by == "token_count"
    # 2 messages (6000 tokens) fits, 3rd would push to 9000 → stops.
    assert len(window.messages) == 2
    assert window.total_tokens == 6000


async def test_token_count_fallback_uses_char_heuristic() -> None:
    """NULL token_count → fallback to len(content)/4."""
    from polaris_api.services import conversation

    base = datetime.now(UTC)
    # 4000-char message with NULL token_count → ~1000 tokens estimated.
    long_content = "x" * 4000
    rows = [
        _msg("user", long_content, base + timedelta(seconds=i), tc=None)
        for i in range(10)
    ]
    conn = _make_conn(rows)

    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
        max_messages=20, max_tokens=4000,
    )

    # ~1000 tokens/message, 4000-token cap → 4 messages fit; 5th would
    # push to ~5000 → stops at 4.
    assert window.truncated_by == "token_count"
    assert len(window.messages) == 4


async def test_message_cap_ties_break_predictably() -> None:
    """Exactly max_messages and under token cap → 'message_count' truncation only if MORE existed."""
    from polaris_api.services import conversation

    base = datetime.now(UTC)
    # Exactly 20 messages, default max_messages=20 → all kept, no truncation.
    rows = [
        _msg("user", f"m{i}", base + timedelta(seconds=i), tc=10)
        for i in range(20)
    ]
    conn = _make_conn(rows)

    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
        max_messages=20,
    )

    assert len(window.messages) == 20
    assert window.truncated_by == "none"


async def test_empty_input_returns_none_truncation() -> None:
    """No messages → empty window, truncated='none', tokens=0."""
    from polaris_api.services import conversation

    conn = _make_conn([])
    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
    )

    assert window.messages == []
    assert window.truncated_by == "none"
    assert window.total_tokens == 0


async def test_total_tokens_field_matches_kept_sum() -> None:
    """ConversationWindow.total_tokens equals the sum of kept token_counts."""
    from polaris_api.services import conversation

    base = datetime.now(UTC)
    rows = [
        _msg("user", "a", base + timedelta(seconds=0), tc=100),
        _msg("assistant", "b", base + timedelta(seconds=1), tc=200),
        _msg("user", "c", base + timedelta(seconds=2), tc=50),
    ]
    conn = _make_conn(rows)

    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
    )

    assert len(window.messages) == 3
    assert window.total_tokens == 350


async def test_messages_returned_chronological_oldest_first() -> None:
    """Output ordering: oldest first (Anthropic SDK messages format)."""
    from polaris_api.services import conversation

    base = datetime.now(UTC)
    rows = [
        _msg("user", "oldest", base + timedelta(seconds=0), tc=5),
        _msg("assistant", "middle", base + timedelta(seconds=1), tc=5),
        _msg("user", "newest", base + timedelta(seconds=2), tc=5),
    ]
    conn = _make_conn(rows)

    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
    )

    assert [m.content for m in window.messages] == ["oldest", "middle", "newest"]
