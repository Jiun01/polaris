"""DB-backed coverage for human-agent reply persistence (phase-10).

Exercises ``services/chatwoot/agent_reply.persist_human_agent_reply`` against a
real Postgres so the SQL (RLS GUC + the ``messages`` partial-unique ON CONFLICT)
is verified, not mocked:

  * a genuine human-agent reply lands ONE ``role='assistant'`` row tagged
    ``metadata.author_type='human_agent'`` with the Chatwoot message id;
  * a second delivery of the SAME Chatwoot message is an idempotent no-op
    (agent-bot webhook retry → no double-persist);
  * a reply whose Polaris conversation doesn't exist yet is skipped, not errored.

Skips without ``POLARIS_DATABASE_URL`` / ``SUPABASE_DB_URL`` (matches
``test_chatwoot_router``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from polaris_api.config import get_settings
from polaris_api.db import pools as pool_module
from polaris_api.services.chatwoot import agent_reply

_TEST_TENANT_PREFIX = "pytest-cwagent-"


def _dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest.fixture
def configured_settings() -> Iterator[None]:
    get_settings.cache_clear()
    settings = get_settings()
    dsn = _dsn() or ""
    settings.polaris_db_url_anon = dsn
    settings.polaris_db_url_service = dsn
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def pools_open(configured_settings: None) -> AsyncIterator[None]:
    _ = configured_settings
    if not _dsn():
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL to run this test")
    await pool_module.close_pools()
    await pool_module.init_pools()
    try:
        yield
    finally:
        await pool_module.close_pools()


@pytest_asyncio.fixture
async def seed_conn(pools_open: None) -> AsyncIterator[asyncpg.Connection]:
    _ = pools_open
    dsn = _dsn()
    assert dsn is not None
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        try:
            await conn.execute(
                f"""
                DELETE FROM messages WHERE tenant_id IN (
                  SELECT id FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'
                )
                """  # noqa: S608
            )
            await conn.execute(
                f"""
                DELETE FROM conversations WHERE tenant_id IN (
                  SELECT id FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'
                )
                """  # noqa: S608
            )
            await conn.execute(
                f"DELETE FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'"  # noqa: S608
            )
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass
        await conn.close()


async def _seed_tenant_and_conversation(
    conn: asyncpg.Connection, *, cw_conv_id: int
) -> tuple[uuid.UUID, uuid.UUID]:
    slug = f"{_TEST_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug
    )
    conversation_id = await conn.fetchval(
        """
        INSERT INTO conversations
            (tenant_id, channel, channel_user_id, chatwoot_conversation_id)
        VALUES ($1, 'whatsapp', $2, $3)
        RETURNING id
        """,
        tenant_id,
        f"6011{uuid.uuid4().int % 10_000_000:07d}",
        cw_conv_id,
    )
    return tenant_id, conversation_id


def _payload(*, cw_conv_id: int, msg_id: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": msg_id,
        "message_type": "outgoing",
        "sender": {"type": "agent", "id": 9, "name": "Aisha"},
        "inbox": {"id": 7},
        "conversation": {"id": cw_conv_id},
        "account": {"id": 1},
        "content": "Your order ships tomorrow.",
        "created_at": "2026-06-13T03:00:00Z",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_persists_one_human_agent_row(seed_conn: asyncpg.Connection) -> None:
    cw_conv_id = uuid.uuid4().int % 1_000_000_000
    tenant_id, conversation_id = await _seed_tenant_and_conversation(
        seed_conn, cw_conv_id=cw_conv_id
    )

    inserted = await agent_reply.persist_human_agent_reply(
        tenant_id=tenant_id,
        chatwoot_conversation_id=cw_conv_id,
        payload=_payload(cw_conv_id=cw_conv_id, msg_id=4242),
    )
    assert inserted is True

    rows = await seed_conn.fetch(
        "SELECT role, content, provider_message_id, chatwoot_message_id, metadata "
        "FROM messages WHERE conversation_id = $1",
        conversation_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "assistant"
    assert row["content"] == "Your order ships tomorrow."
    assert row["provider_message_id"] == "chatwoot.4242"
    assert row["chatwoot_message_id"] == 4242
    meta = row["metadata"]
    if isinstance(meta, str):  # codec may hand back raw json on this conn
        import json

        meta = json.loads(meta)
    assert meta["author_type"] == "human_agent"
    assert meta["agent_name"] == "Aisha"


@pytest.mark.asyncio
async def test_retry_is_idempotent(seed_conn: asyncpg.Connection) -> None:
    cw_conv_id = uuid.uuid4().int % 1_000_000_000
    tenant_id, conversation_id = await _seed_tenant_and_conversation(
        seed_conn, cw_conv_id=cw_conv_id
    )
    payload = _payload(cw_conv_id=cw_conv_id, msg_id=5555)

    first = await agent_reply.persist_human_agent_reply(
        tenant_id=tenant_id, chatwoot_conversation_id=cw_conv_id, payload=payload
    )
    second = await agent_reply.persist_human_agent_reply(
        tenant_id=tenant_id, chatwoot_conversation_id=cw_conv_id, payload=payload
    )
    assert first is True
    assert second is False  # ON CONFLICT DO NOTHING → no second row

    count = await seed_conn.fetchval(
        "SELECT count(*) FROM messages WHERE conversation_id = $1", conversation_id
    )
    assert count == 1


@pytest.mark.asyncio
async def test_missing_conversation_is_skipped(
    seed_conn: asyncpg.Connection,
) -> None:
    # A tenant with NO linked Polaris conversation for this cw_conv_id (human
    # replied before any bot turn) → skip, not error, no row.
    slug = f"{_TEST_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    tenant_id = await seed_conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug
    )
    cw_conv_id = uuid.uuid4().int % 1_000_000_000

    inserted = await agent_reply.persist_human_agent_reply(
        tenant_id=tenant_id,
        chatwoot_conversation_id=cw_conv_id,
        payload=_payload(cw_conv_id=cw_conv_id, msg_id=7777),
    )
    assert inserted is False

    count = await seed_conn.fetchval(
        "SELECT count(*) FROM messages WHERE tenant_id = $1", tenant_id
    )
    assert count == 0
