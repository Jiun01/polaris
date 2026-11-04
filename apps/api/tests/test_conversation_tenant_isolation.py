"""Tenant-isolation tests for conversation cache — Phase 4 PR-3.

Verifies that the Redis cache key shape (``conv:{tenant_id}:{conversation_id}``)
makes cross-tenant pollution structurally impossible. Pairs with the
RLS-side guard on the messages table — defense in depth.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _make_conn_db_returns(rows: list[dict]) -> AsyncMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=list(reversed(rows)))
    return conn


async def test_two_tenants_share_conversation_id_no_leak(fake_redis) -> None:
    """Two tenants happen to have the same conversation_id (collision).

    This is rare but possible (UUID v4 collisions are astronomically
    unlikely; the test guards against a future migration that might
    use deterministic IDs). Each tenant must see only their own
    cached messages.
    """
    from polaris_api.services import conversation

    tenant_a = uuid4()
    tenant_b = uuid4()
    shared_conv = uuid4()

    # Pre-populate cache for both tenants under the same conversation_id.
    await fake_redis.set(f"conv:{tenant_a}:{shared_conv}", json.dumps([{
        "role": "user", "content": "tenant-A-secret",
        "created_at": datetime.now(UTC).isoformat(),
        "token_count": None,
    }]))
    await fake_redis.set(f"conv:{tenant_b}:{shared_conv}", json.dumps([{
        "role": "user", "content": "tenant-B-secret",
        "created_at": datetime.now(UTC).isoformat(),
        "token_count": None,
    }]))

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    window_a = await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_a, conversation_id=shared_conv,
    )
    window_b = await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_b, conversation_id=shared_conv,
    )

    assert window_a.messages[0].content == "tenant-A-secret"
    assert window_b.messages[0].content == "tenant-B-secret"
    # Neither should leak the other's content.
    assert "B-secret" not in window_a.messages[0].content
    assert "A-secret" not in window_b.messages[0].content


async def test_save_does_not_overwrite_other_tenants_cache(fake_redis) -> None:
    """Tenant A saving a message does NOT touch tenant B's cache entry."""
    from polaris_api.services import conversation

    tenant_a = uuid4()
    tenant_b = uuid4()
    conv_a = uuid4()
    conv_b = uuid4()

    # Tenant B has cached state.
    b_payload = json.dumps([{
        "role": "user", "content": "B's earlier msg",
        "created_at": datetime.now(UTC).isoformat(),
        "token_count": None,
    }])
    await fake_redis.set(f"conv:{tenant_b}:{conv_b}", b_payload)

    # Tenant A writes a message in a different conversation.
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": uuid4(), "created_at": datetime.now(UTC),
    })
    await conversation.save_message(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_a, conversation_id=conv_a,
        role="user", content="A's new msg",
    )

    # Tenant B's cache is intact.
    b_after = await fake_redis.get(f"conv:{tenant_b}:{conv_b}")
    assert b_after is not None
    b_parsed = json.loads(b_after)
    assert len(b_parsed) == 1
    assert b_parsed[0]["content"] == "B's earlier msg"


async def test_cache_key_format_tenant_first(fake_redis) -> None:
    """Cache key starts with tenant_id — reviewers can grep by tenant."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()

    conn = _make_conn_db_returns([{
        "role": "user", "content": "x",
        "created_at": datetime.now(UTC),
        "token_count": None,
    }])
    await conversation.load_history(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
    )

    keys = []
    async for k in fake_redis.scan_iter(match=f"conv:{tenant_id}:*"):
        keys.append(k)

    assert len(keys) == 1
    key_str = keys[0].decode() if isinstance(keys[0], bytes) else keys[0]
    # Tenant id is the FIRST segment after the prefix.
    assert key_str.startswith(f"conv:{tenant_id}:")
    assert str(conv_id) in key_str


async def test_db_read_filters_by_tenant_id(fake_redis) -> None:
    """The SELECT WHERE clause filters on tenant_id — RLS double-guard.

    Even if a future bug let a wrong tenant_id slip through, the SQL
    predicate would still scope the read.
    """
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    await conversation.load_history(
        conn=conn, redis=None,  # force DB path
        tenant_id=tenant_id, conversation_id=conv_id,
    )

    sql, *params = conn.fetch.call_args.args
    assert "tenant_id" in sql.lower()
    assert "conversation_id" in sql.lower()
    # First param is tenant_id, second is conversation_id.
    assert params[0] == tenant_id
    assert params[1] == conv_id


async def test_db_insert_includes_tenant_id_in_first_position(fake_redis) -> None:
    """Save's INSERT params position tenant_id first — RLS double-guard."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": uuid4(), "created_at": datetime.now(UTC),
    })

    await conversation.save_message(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=uuid4(),
        role="user", content="x",
    )

    sql, *params = conn.fetchrow.call_args.args
    assert "INSERT INTO messages" in sql
    assert "tenant_id" in sql
    # First $-param is tenant_id per the SQL shape.
    assert params[0] == tenant_id
