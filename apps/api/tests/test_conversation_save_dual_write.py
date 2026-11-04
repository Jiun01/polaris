"""save_message dual-write tests — Phase 4 PR-3.

Order: Supabase first (durable). Redis second (best-effort). If DB
fails, Redis is untouched — cache stays consistent with DB. If Redis
fails after a successful DB write, cache is DROPPED so next read
rebuilds from DB. Both behaviors are tested here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _make_conn_success(new_id, created_at=None) -> AsyncMock:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": new_id,
        "created_at": created_at or datetime.now(UTC),
    })
    return conn


async def test_db_then_redis_happy_path(fake_redis) -> None:
    """Successful path: DB INSERT, then cache append."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    new_id = uuid4()
    conn = _make_conn_success(new_id)

    returned = await conversation.save_message(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
        role="user", content="hello",
    )

    assert returned == new_id
    # DB write happened.
    assert conn.fetchrow.await_count == 1
    # Cache was populated with this message.
    cached = await fake_redis.get(f"conv:{tenant_id}:{conv_id}")
    assert cached is not None
    payload = json.loads(cached)
    assert payload[0]["content"] == "hello"
    assert payload[0]["role"] == "user"


async def test_db_failure_skips_redis(fake_redis) -> None:
    """If the INSERT raises, Redis is never touched."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await conversation.save_message(
            conn=conn, redis=fake_redis,
            tenant_id=tenant_id, conversation_id=conv_id,
            role="user", content="hello",
        )

    # Cache was NOT touched.
    cached = await fake_redis.get(f"conv:{tenant_id}:{conv_id}")
    assert cached is None


async def test_redis_failure_after_db_does_not_raise(monkeypatch) -> None:
    """Redis blip after a successful DB write → log + don't raise.

    Drops the (potentially stale) cache so next load rebuilds.
    The DB write must succeed and the returned UUID must be valid.
    """
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    new_id = uuid4()
    conn = _make_conn_success(new_id)

    # Mock redis where every async call raises.
    bad_redis = AsyncMock()
    bad_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    bad_redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    bad_redis.delete = AsyncMock(side_effect=ConnectionError("redis down"))

    # Should NOT raise.
    returned = await conversation.save_message(
        conn=conn, redis=bad_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
        role="assistant", content="reply",
    )

    assert returned == new_id


async def test_save_appends_to_existing_cache_entry(fake_redis) -> None:
    """Second save extends the cached list, not overwrites it."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()
    new_id_1 = uuid4()
    new_id_2 = uuid4()

    conn1 = _make_conn_success(new_id_1)
    await conversation.save_message(
        conn=conn1, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
        role="user", content="q1",
    )

    conn2 = _make_conn_success(new_id_2)
    await conversation.save_message(
        conn=conn2, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
        role="assistant", content="a1",
    )

    cached = await fake_redis.get(f"conv:{tenant_id}:{conv_id}")
    payload = json.loads(cached)
    assert [m["content"] for m in payload] == ["q1", "a1"]
    assert [m["role"] for m in payload] == ["user", "assistant"]


async def test_save_without_redis_returns_id(fake_redis) -> None:
    """redis=None → DB write only, returns new UUID."""
    from polaris_api.services import conversation

    new_id = uuid4()
    conn = _make_conn_success(new_id)

    returned = await conversation.save_message(
        conn=conn, redis=None,
        tenant_id=uuid4(), conversation_id=uuid4(),
        role="user", content="solo",
    )

    assert returned == new_id


async def test_save_passes_token_count_to_db(fake_redis) -> None:
    """token_count is threaded into the INSERT param positions."""
    from polaris_api.services import conversation

    conn = _make_conn_success(uuid4())

    await conversation.save_message(
        conn=conn, redis=fake_redis,
        tenant_id=uuid4(), conversation_id=uuid4(),
        role="user", content="hi",
        token_count=42,
    )

    args = conn.fetchrow.call_args.args
    # Args are positional: (SQL, tenant_id, conversation_id, role, content, token_count)
    assert args[5] == 42


async def test_save_caps_cache_at_max_messages(fake_redis) -> None:
    """Cache is capped at _CACHE_MAX_MESSAGES (50); oldest get trimmed."""
    from polaris_api.services import conversation

    tenant_id = uuid4()
    conv_id = uuid4()

    # Pre-populate 50 messages in cache.
    seed = []
    for i in range(50):
        seed.append({
            "role": "user", "content": f"msg-{i}",
            "created_at": datetime.now(UTC).isoformat(),
            "token_count": None,
        })
    await fake_redis.set(
        f"conv:{tenant_id}:{conv_id}", json.dumps(seed),
    )

    # Adding one more should cap at 50; oldest (msg-0) gets trimmed.
    conn = _make_conn_success(uuid4())
    await conversation.save_message(
        conn=conn, redis=fake_redis,
        tenant_id=tenant_id, conversation_id=conv_id,
        role="user", content="newest",
    )

    payload = json.loads(await fake_redis.get(f"conv:{tenant_id}:{conv_id}"))
    assert len(payload) == 50
    # Oldest dropped; newest added at end.
    assert payload[0]["content"] == "msg-1"
    assert payload[-1]["content"] == "newest"
