"""load_history tests — Phase 4 PR-3.

Covers: cache hit path, cache miss → DB → cache rehydrate, empty
conversation, corrupt cache recovery, TTL refresh on read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _row(role: str, content: str, *, tc: int | None = None) -> dict:
    return {
        "role": role,
        "content": content,
        "created_at": datetime.now(UTC),
        "token_count": tc,
    }


def _make_conn(*, db_rows: list[dict]) -> AsyncMock:
    """Stub asyncpg.Connection returning ``db_rows`` for SELECT messages.

    asyncpg returns DESC ordering from the loader's query; the
    fixture mimics that — most-recent first.
    """
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=list(reversed(db_rows)))
    return conn


async def test_cache_hit_skips_db(fake_redis) -> None:
    """When Redis has a fresh entry, fetch is never called on the DB."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    cached = json.dumps([{
        "role": "user", "content": "hi from cache",
        "created_at": datetime.now(UTC).isoformat(),
        "token_count": 4,
    }])
    await fake_redis.set(f"conv:{tenant_id}:{conv_id}", cached)

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    window = await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
    )

    assert window.messages[0].content == "hi from cache"
    conn.fetch.assert_not_called()


async def test_cache_miss_loads_from_db_and_rehydrates(fake_redis) -> None:
    """Cache empty → DB SELECT → Redis SET with full payload + TTL."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    conn = _make_conn(db_rows=[
        _row("user", "msg-1"),
        _row("assistant", "reply-1"),
        _row("user", "msg-2"),
    ])

    window = await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
    )

    assert [m.content for m in window.messages] == ["msg-1", "reply-1", "msg-2"]
    conn.fetch.assert_awaited_once()

    # Cache populated.
    cached = await fake_redis.get(f"conv:{tenant_id}:{conv_id}")
    assert cached is not None
    parsed = json.loads(cached)
    assert len(parsed) == 3


async def test_empty_conversation_returns_empty_window(fake_redis) -> None:
    """No DB rows + no cache → empty window, truncated_by='none'."""
    from polaris_api.services import conversation

    conn = _make_conn(db_rows=[])
    window = await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=uuid4(), conversation_id=uuid4(),
    )

    assert window.messages == []
    assert window.truncated_by == "none"
    assert window.total_tokens == 0


async def test_corrupt_cache_falls_back_to_db(fake_redis) -> None:
    """Garbage in Redis → log + treat as miss, DB hydrates fresh."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    await fake_redis.set(
        f"conv:{tenant_id}:{conv_id}", "not valid json {",
    )

    conn = _make_conn(db_rows=[_row("user", "from db")])
    window = await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
    )

    assert [m.content for m in window.messages] == ["from db"]
    conn.fetch.assert_awaited_once()


async def test_load_without_redis_works(fake_redis) -> None:
    """Passing redis=None just skips the cache layer; DB still hits."""
    from polaris_api.services import conversation

    conn = _make_conn(db_rows=[_row("user", "no-cache")])
    window = await conversation.load_history(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
    )

    assert [m.content for m in window.messages] == ["no-cache"]


async def test_cache_query_uses_tenant_scoped_key(fake_redis) -> None:
    """The Redis key includes tenant_id FIRST so two tenants can't collide."""
    from polaris_api.services import conversation

    tenant_a = uuid4()
    tenant_b = uuid4()
    same_conv_id = uuid4()  # same UUID, different tenants

    await fake_redis.set(f"conv:{tenant_a}:{same_conv_id}", json.dumps([{
        "role": "user", "content": "from A",
        "created_at": datetime.now(UTC).isoformat(),
        "token_count": None,
    }]))

    conn = _make_conn(db_rows=[_row("user", "from B db")])

    # Tenant B should NOT see tenant A's cached entry.
    window_b = await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_b, conversation_id=same_conv_id,
    )
    assert [m.content for m in window_b.messages] == ["from B db"]


async def test_ttl_refreshed_on_read(fake_redis) -> None:
    """Reading a cached entry bumps its TTL back to 24h."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    key = f"conv:{tenant_id}:{conv_id}"
    await fake_redis.set(key, json.dumps([{
        "role": "user", "content": "warm",
        "created_at": datetime.now(UTC).isoformat(),
        "token_count": None,
    }]), ex=100)  # only 100s left

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
    )

    ttl = await fake_redis.ttl(key)
    assert ttl > 100  # bumped from 100 → 86400
