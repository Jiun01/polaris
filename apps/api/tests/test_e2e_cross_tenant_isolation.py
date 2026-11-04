"""E2E sweep gap fill — cross-tenant isolation under the live pipeline.

Phase 1 RLS tests prove SQL-level isolation. Phase 4
test_conversation_tenant_isolation.py proves the conversation layer
respects tenant boundaries. Neither traces a message through the
full ``pipeline.run`` and asserts the RAG retrieval didn't leak
chunks across tenants.

This test seeds two tenants each with a distinct keyword in their
sole knowledge_chunk, runs the pipeline for tenant A, and asserts:

  1. The persisted ``messages.rag_chunk_ids`` for A contains ONLY
     A's chunk id (zero leakage of B's chunk id).
  2. The system prompt actually sent to Anthropic contains A's
     keyword and never B's — proving the RAG context formatter
     received tenant-scoped results, not just that the column
     stored the right ids.

Symmetric assertion run for B in the second test ensures the
isolation isn't directionally biased (e.g. "first seeded wins").

Mocked LLM, real DB (skips without SUPABASE_DB_URL).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import fakeredis.aioredis
import pytest
import pytest_asyncio
from polaris_api.config import get_settings
from polaris_api.db import pools as pool_module
from polaris_api.services import embed as embed_module
from polaris_api.services import pipeline as pipeline_module
from polaris_shared_types import IncomingMessage, SenderRef

_TEST_TENANT_PREFIX = "pytest-e2e-iso-"

# Keywords picked to be lexically unique so a BM25 hit can only come
# from the matching tenant's chunk.
_ALPHA_MARKER = "xkliop-alpha-marker-7421"
_BETA_MARKER = "xkliop-beta-marker-8932"


def _dsn() -> str | None:
    # pydantic-settings auto-loads from .env at repo root, so the operator
    # doesn't have to source it before pytest. CI env-var overrides still
    # win (Settings reads os.environ as the highest-priority source).
    s = get_settings()
    return s.polaris_db_url_service or s.polaris_db_url_anon or None


@pytest.fixture
def configured_settings() -> Iterator[None]:
    """Use the REAL polaris_anon / polaris_service pools so RLS is
    the actual gate for the cross-tenant isolation assertion (not just
    the RAG SQL's tenant_id predicate). Stronger E2E claim.
    """
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture(loop_scope="function")
async def pools_open(configured_settings: None) -> AsyncIterator[None]:
    _ = configured_settings
    if not _dsn():
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL to run E2E sweep")
    # CI follow-up (2026-05-19): see test_e2e_webhook_to_messages.py
    # for the full reason — POLARIS_DB_URL_ANON not bootstrapped in
    # pr-check.yml. Tracked separately.
    if (
        (os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true")
        and not os.environ.get("POLARIS_DB_URL_ANON")
    ):
        pytest.skip(
            "CI follow-up: POLARIS_DB_URL_ANON not bootstrapped; tracked separately."
        )
    await pool_module.close_pools()
    await pool_module.init_pools()
    try:
        yield
    finally:
        await pool_module.close_pools()


@pytest_asyncio.fixture(loop_scope="function")
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
                f"DELETE FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'"  # noqa: S608
            )
        except Exception:  # noqa: BLE001, S110
            pass
        await conn.close()


@pytest_asyncio.fixture(loop_scope="function")
async def fake_redis_e2e() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    """Function-scoped fakeredis for the pipeline + zero-vector embed.

    Distinct from the conftest ``fake_redis`` only in that we don't
    need monkeypatching against the routers here — pipeline takes
    redis through ``deps`` directly.
    """
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield fake
    finally:
        await fake.aclose()


@pytest.fixture(autouse=True)
def _zero_vector_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    embed_module._reset_client_for_testing()

    async def _zero_vec(_text: str, **_kwargs: Any) -> list[float]:
        # **_kwargs absorbs embed_query's tenant_id (and any future) kwarg.
        return [0.0] * 1024

    monkeypatch.setattr(embed_module, "embed_query", _zero_vec)


async def _seed_tenant_with_marker(
    conn: asyncpg.Connection,
    *,
    suffix: str,
    marker: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns (tenant_id, chunk_id, channel_id)."""
    slug = f"{_TEST_TENANT_PREFIX}{suffix}"
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug,
    )
    # Channel + quotas. The channel id is REQUIRED now: migration 106 added a
    # conversations.channel_id FK → tenant_channels, so the conversation the
    # pipeline inserts must reference this real channel (not a random uuid).
    channel_id = await conn.fetchval(
        """
        INSERT INTO tenant_channels
            (tenant_id, channel, chatwoot_inbox_id, waba_id, phone_number_id, status)
        VALUES ($1, 'whatsapp', $2, $3, $4, 'active')
        RETURNING id
        """,
        tenant_id,
        abs(hash(slug)) % 1_000_000_000,
        f"iso-waba-{suffix}",
        f"iso-pid-{suffix}",
    )
    await conn.execute(
        """
        INSERT INTO tenant_quotas (
            tenant_id, bot_replies, conversation_retention_days,
            allowed_channel_kinds, outbound_broadcast_enabled,
            multi_branch_enabled, knowledge_chunks_total_mb,
            rate_limit_per_minute, monthly_llm_tokens_in,
            monthly_llm_tokens_out, monthly_ingestion_pages,
            monthly_broadcast_recipients, monthly_bookings,
            bot_replies_period_cap
        ) VALUES ($1, 10000, 90, ARRAY['whatsapp'], false, false,
                  100, 60, 1000000, 1000000, 1000, 10000, 1000, 10000)
        """,
        tenant_id,
    )

    chunk_text = f"office hours information including {marker} keyword"
    request_id = await conn.fetchval(
        """
        INSERT INTO ingestion_requests
            (tenant_id, source_type, source_text, status)
        VALUES ($1, 'text', $2, 'completed')
        RETURNING id
        """,
        tenant_id, chunk_text,
    )
    zero_vec_literal = "[" + ",".join(["0"] * 1024) + "]"
    chunk_id = await conn.fetchval(
        """
        INSERT INTO knowledge_chunks
            (tenant_id, request_id, source_name, source_type,
             chunk_index, content, content_hash, embedding, token_count)
        VALUES ($1, $2, $3, 'text', 0, $4, $5, $6::vector, $7)
        RETURNING id
        """,
        tenant_id, request_id, f"iso-{suffix}.md",
        chunk_text, hashlib.sha256(chunk_text.encode()).hexdigest(),
        zero_vec_literal, max(1, len(chunk_text) // 4),
    )
    return tenant_id, chunk_id, channel_id


def _make_stub_meta_client() -> Any:
    import time as _time

    client = AsyncMock()
    run_id = _time.monotonic_ns()
    counter = {"n": 0}

    async def fake_send_text(*, phone_number_id: str, to: str, body: str) -> str:
        counter["n"] += 1
        return f"wamid.iso.{run_id}.{counter['n']:04d}"

    client.send_text = fake_send_text
    return client


def _extract_prompt_text(call: Any) -> str:
    """Flatten an Anthropic messages.create call's kwargs into one
    string so we can search it for keywords."""
    parts: list[str] = []
    kwargs = call.kwargs
    system = kwargs.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for block in system:
            txt = block.get("text") if isinstance(block, dict) else None
            if isinstance(txt, str):
                parts.append(txt)
    messages = kwargs.get("messages") or []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                txt = block.get("text") if isinstance(block, dict) else None
                if isinstance(txt, str):
                    parts.append(txt)
    return "\n".join(parts)


async def _run_pipeline_for(
    *,
    tenant_id: uuid.UUID,
    channel_id: uuid.UUID,
    user_text: str,
    fake_redis_e2e: fakeredis.aioredis.FakeRedis,
) -> Any:
    from datetime import UTC, datetime

    incoming = IncomingMessage(
        tenant_id=tenant_id,
        channel_id=channel_id,  # migration 106: conversations.channel_id FK
        provider="whatsapp",
        provider_message_id=f"iso-{uuid.uuid4().hex[:10]}",
        sender=SenderRef(channel_user_id="60111000000", display_name="Iso Tester"),
        body=user_text,
        received_at=datetime.now(UTC),
    )
    deps = pipeline_module.PipelineDeps(
        anon_pool=pool_module.get_anon_pool(),
        redis=fake_redis_e2e,
        meta_client=_make_stub_meta_client(),
    )
    return await pipeline_module.run(incoming=incoming, deps=deps)


@pytest.mark.asyncio(loop_scope="function")
async def test_tenant_a_pipeline_does_not_leak_tenant_b_chunks(
    seed_conn: asyncpg.Connection,
    fake_redis_e2e: fakeredis.aioredis.FakeRedis,
    mock_anthropic: MagicMock,
    mock_cohere: MagicMock,
) -> None:
    """Pipeline run for A sees only A's chunks in both rag_chunk_ids
    and the system prompt sent to Anthropic. B's marker never appears.
    """
    from tests.conftest import make_mock_anthropic_message

    suffix = uuid.uuid4().hex[:8]
    tenant_a, chunk_a, channel_a = await _seed_tenant_with_marker(
        seed_conn, suffix=f"a-{suffix}", marker=_ALPHA_MARKER,
    )
    tenant_b, chunk_b, channel_b = await _seed_tenant_with_marker(
        seed_conn, suffix=f"b-{suffix}", marker=_BETA_MARKER,
    )

    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="here are A's hours", output_tokens=8),
    ]

    decision = await _run_pipeline_for(
        tenant_id=tenant_a,
        channel_id=channel_a,
        user_text=f"tell me about {_ALPHA_MARKER}",
        fake_redis_e2e=fake_redis_e2e,
    )
    assert decision.tenant_id == tenant_a

    # --- DB invariant: rag_chunk_ids contains A's chunk, never B's ---
    msg_row = await seed_conn.fetchrow(
        """
        SELECT rag_chunk_ids FROM messages
         WHERE tenant_id = $1 AND role = 'assistant'
         ORDER BY created_at DESC LIMIT 1
        """,
        tenant_a,
    )
    assert msg_row is not None
    ids = msg_row["rag_chunk_ids"] or []
    assert chunk_a in ids, f"expected A's chunk {chunk_a} in {ids}"
    assert chunk_b not in ids, f"LEAK: tenant B's chunk {chunk_b} appeared in {ids}"

    # --- Anthropic-prompt invariant: A's marker present, B's absent ---
    # Two calls: classify + generate. Only the generate call carries
    # the RAG context block.
    generate_call = mock_anthropic.messages.create.call_args_list[1]
    prompt_text = _extract_prompt_text(generate_call)
    assert _ALPHA_MARKER in prompt_text, "tenant A's marker missing from generate prompt"
    assert _BETA_MARKER not in prompt_text, "LEAK: tenant B's marker in tenant A's generate prompt"


@pytest.mark.asyncio(loop_scope="function")
async def test_tenant_b_pipeline_does_not_leak_tenant_a_chunks(
    seed_conn: asyncpg.Connection,
    fake_redis_e2e: fakeredis.aioredis.FakeRedis,
    mock_anthropic: MagicMock,
    mock_cohere: MagicMock,
) -> None:
    """Symmetric direction — run pipeline for B, assert A's marker
    never appears. Guards against a directional bug like 'first
    seeded tenant always wins the SET LOCAL'."""
    from tests.conftest import make_mock_anthropic_message

    suffix = uuid.uuid4().hex[:8]
    # Seed A FIRST so an ordering bug would surface A's marker in B's run.
    tenant_a, chunk_a, channel_a = await _seed_tenant_with_marker(
        seed_conn, suffix=f"a-{suffix}", marker=_ALPHA_MARKER,
    )
    tenant_b, chunk_b, channel_b = await _seed_tenant_with_marker(
        seed_conn, suffix=f"b-{suffix}", marker=_BETA_MARKER,
    )

    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="here are B's hours", output_tokens=8),
    ]

    decision = await _run_pipeline_for(
        tenant_id=tenant_b,
        channel_id=channel_b,
        user_text=f"tell me about {_BETA_MARKER}",
        fake_redis_e2e=fake_redis_e2e,
    )
    assert decision.tenant_id == tenant_b

    msg_row = await seed_conn.fetchrow(
        """
        SELECT rag_chunk_ids FROM messages
         WHERE tenant_id = $1 AND role = 'assistant'
         ORDER BY created_at DESC LIMIT 1
        """,
        tenant_b,
    )
    assert msg_row is not None
    ids = msg_row["rag_chunk_ids"] or []
    assert chunk_b in ids
    assert chunk_a not in ids, f"LEAK: tenant A's chunk {chunk_a} in tenant B's response"

    generate_call = mock_anthropic.messages.create.call_args_list[1]
    prompt_text = _extract_prompt_text(generate_call)
    assert _BETA_MARKER in prompt_text
    assert _ALPHA_MARKER not in prompt_text, "LEAK: tenant A's marker in tenant B's generate prompt"
    _ = tenant_a  # explicitly referenced to satisfy lint
