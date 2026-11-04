"""Pipeline orchestrator end-to-end tests — Phase 4 PR-4.

Walks one IncomingMessage through the full pipeline with mocked
clients (asyncpg pool, Redis, Cohere, Anthropic, MetaWhatsApp).
Verifies the SHAPE of the orchestration: conversation upsert, user
msg save, classify, RAG, history load, generate, assistant persist,
dispatch, and the returned PipelineDecision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from tests.conftest import make_mock_anthropic_message

pytestmark = pytest.mark.asyncio


class _FakeTransaction:
    """asyncpg.Connection.transaction() — async context manager."""

    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakeAcquire:
    """asyncpg.Pool.acquire() — async context manager yielding conn."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


def _make_pool_with_conn() -> tuple[MagicMock, AsyncMock]:
    """Return (pool, conn) where pool.acquire() yields conn."""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_FakeTransaction())

    # Default fetchrow router: returns sensible defaults per SQL fragment.
    new_conv_id = uuid4()
    new_user_msg_id = uuid4()
    new_assistant_msg_id = uuid4()
    tier_row = {"plan_tier": "starter", "cohere_rerank_enabled": None}
    has_chunks_row = {"has": False}

    async def fetchrow(sql: str, *args: Any, **kwargs: Any) -> Any:
        s = sql.strip()
        if "INSERT INTO conversations" in s:
            return {"id": new_conv_id, "tenant_branch_id": None}
        if "FROM tenants" in s:
            return tier_row
        if "FROM knowledge_chunks" in s:
            return has_chunks_row
        if "INSERT INTO messages" in s and "'assistant'" in s:
            return {"id": new_assistant_msg_id, "created_at": datetime.now(UTC)}
        if "INSERT INTO messages" in s and "RETURNING id, created_at" in s:
            # Generic INSERT (used by conversation.save_message for user msg)
            return {"id": new_user_msg_id, "created_at": datetime.now(UTC)}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=[])  # empty conversation history
    conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _FakeAcquire(conn))

    # Stash ids on the pool for assertions.
    pool._conv_id = new_conv_id
    pool._user_msg_id = new_user_msg_id
    pool._assistant_msg_id = new_assistant_msg_id

    return pool, conn


def _make_meta_client(*, send_returns: str | None = "wamid.test123") -> MagicMock:
    meta = MagicMock()
    if send_returns is not None:
        meta.send_text = AsyncMock(return_value=send_returns)
    else:
        meta.send_text = AsyncMock(side_effect=NotImplementedError("Phase 6"))
    return meta


def _make_incoming(**overrides: Any) -> Any:
    from polaris_shared_types import IncomingMessage, SenderRef

    defaults = {
        "tenant_id": uuid4(),
        "channel_id": uuid4(),
        "provider": "whatsapp",
        "provider_message_id": "wamid.in.001",
        "sender": SenderRef(channel_user_id="60123456789", display_name="Test User"),
        "body": "what time you open",
        "received_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return IncomingMessage(**defaults)


async def test_happy_path_returns_pipeline_decision(
    mock_anthropic: MagicMock, fake_redis: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full pipeline: classify=simple → Haiku reply → dispatch."""
    from polaris_api.services import pipeline
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, conn = _make_pool_with_conn()
    meta = _make_meta_client()
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=meta)
    # Configured WhatsApp tenant: per-tenant WABA creds resolve (the dispatch no
    # longer falls back to the shared demo number for a real 'whatsapp' channel).
    monkeypatch.setattr(
        pipeline, "_resolve_whatsapp_credentials",
        AsyncMock(return_value=("tenant-pid", "tenant-tok")),
    )

    # Classifier returns 'simple'; generate returns short text.
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),  # classify
        make_mock_anthropic_message(  # generate
            text="We open at 9am.", input_tokens=120, output_tokens=15,
            cache_creation_input_tokens=80, cache_read_input_tokens=0,
        ),
    ]

    incoming = _make_incoming()
    decision = await run(incoming=incoming, deps=deps)

    assert decision.tenant_id == incoming.tenant_id
    assert decision.conversation_id == pool._conv_id
    assert decision.user_message_id == pool._user_msg_id
    assert decision.assistant_message_id == pool._assistant_msg_id
    assert decision.response.content == "We open at 9am."
    assert decision.response.intent == "simple"
    assert decision.response.model_used == "haiku"
    assert decision.provider_message_id == "wamid.test123"
    assert decision.handoff_emitted is False
    # SET LOCAL was called for tenant context.
    set_local_calls = [
        c for c in conn.execute.await_args_list
        if "set_config('app.current_tenant_id'" in c.args[0]
    ]
    assert len(set_local_calls) >= 1


async def test_handoff_intent_short_circuits_to_template(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """intent='handoff' → no RAG, no generate_response, handoff template emitted."""
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, conn = _make_pool_with_conn()
    meta = _make_meta_client()
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=meta)

    # Only one Anthropic call (the classifier).
    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="handoff",
    )

    decision = await run(incoming=_make_incoming(), deps=deps)

    assert decision.handoff_emitted is True
    assert decision.response.intent == "handoff"
    assert "human agent" in decision.response.content
    # Classifier ran exactly once; generate_response did NOT.
    assert mock_anthropic.messages.create.await_count == 1


async def test_chatwoot_provider_rejected() -> None:
    """provider='chatwoot' → ValueError (Phase 7's path, not bot pipeline)."""
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, _conn = _make_pool_with_conn()
    deps = PipelineDeps(
        anon_pool=pool, redis=MagicMock(), meta_client=MagicMock(),
    )
    incoming = _make_incoming(provider="chatwoot")

    with pytest.raises(ValueError, match="chatwoot"):
        await run(incoming=incoming, deps=deps)


async def test_media_only_message_routes_to_handoff(
    fake_redis: Any,
) -> None:
    """body=None (media-only) → handoff path, no LLM call."""
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, _conn = _make_pool_with_conn()
    meta = _make_meta_client()
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=meta)

    decision = await run(
        incoming=_make_incoming(body=None), deps=deps,
    )

    assert decision.handoff_emitted is True
    assert "human agent" in decision.response.content


async def test_dispatch_failure_returns_decision_without_wamid(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """Meta send NotImplementedError (e.g. instagram) → decision.provider_message_id=None."""
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, _conn = _make_pool_with_conn()
    meta = _make_meta_client(send_returns=None)  # raises NotImplementedError
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=meta)

    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="ok"),
    ]

    incoming = _make_incoming(provider="messenger")
    decision = await run(incoming=incoming, deps=deps)

    # Pipeline didn't crash; row is committed; just no wamid stamp.
    assert decision.provider_message_id is None
    assert decision.assistant_message_id is not None


async def test_otel_set_local_tenant_id_on_every_run(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """Pipeline always sets app.current_tenant_id GUC for RLS."""
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, conn = _make_pool_with_conn()
    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="ok"),
    ]

    incoming = _make_incoming()
    await run(incoming=incoming, deps=deps)

    set_local_calls = [
        c for c in conn.execute.await_args_list
        if "set_config('app.current_tenant_id'" in c.args[0]
    ]
    # At minimum, the main transaction SET LOCAL. (Stamp UPDATE also
    # sets it but we don't strictly require the count here.)
    assert len(set_local_calls) >= 1
    # Tenant id arg matches.
    assert UUID(set_local_calls[0].args[1]) == incoming.tenant_id


async def test_empty_history_is_dropped_safely(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """First-touch conversation: history is empty before save; pipeline still works."""
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, conn = _make_pool_with_conn()
    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )
    # No history rows in DB.
    conn.fetch = AsyncMock(return_value=[])

    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="first reply"),
    ]

    decision = await run(incoming=_make_incoming(), deps=deps)

    assert decision.response.content == "first reply"


async def test_run_inner_issues_one_tenant_snapshot_call(
    mock_anthropic: MagicMock, fake_redis: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit Batch B (B1): exactly ONE load_tenant_snapshot call per message —
    the agent branch (disabled here) and the classic branch both consume the
    single snapshot loaded near the top of _run_inner instead of re-querying."""
    from polaris_api.services import pipeline
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, _conn = _make_pool_with_conn()
    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )

    snapshot_calls = 0
    real_load = pipeline.tenant_snapshot_svc.load_tenant_snapshot

    async def counting_load(conn: Any, tenant_id: Any) -> Any:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return await real_load(conn, tenant_id)

    monkeypatch.setattr(
        pipeline.tenant_snapshot_svc, "load_tenant_snapshot", counting_load
    )

    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="ok"),
    ]

    await run(incoming=_make_incoming(), deps=deps)

    assert snapshot_calls == 1


async def test_simple_intent_skips_products_and_promos(
    mock_anthropic: MagicMock, fake_redis: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit Batch B (B2): simple-tier classification issues NO products/promo
    lookups."""
    from polaris_api.services import pipeline
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, _conn = _make_pool_with_conn()
    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )

    products_mock = AsyncMock(return_value=[])
    promos_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(
        pipeline.products_search, "search_products_hybrid", products_mock
    )
    monkeypatch.setattr(
        pipeline.promotions_lookup, "fetch_active_promotions", promos_mock
    )

    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="ok"),
    ]

    await run(incoming=_make_incoming(), deps=deps)

    products_mock.assert_not_called()
    promos_mock.assert_not_called()


async def test_medium_intent_calls_products_and_promos(
    mock_anthropic: MagicMock, fake_redis: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit Batch B (B2): medium-tier classification still runs the
    products/promo lookups — behavior-identical for non-simple intents."""
    from polaris_api.services import pipeline
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, _conn = _make_pool_with_conn()
    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )

    products_mock = AsyncMock(return_value=[])
    promos_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(
        pipeline.products_search, "search_products_hybrid", products_mock
    )
    monkeypatch.setattr(
        pipeline.promotions_lookup, "fetch_active_promotions", promos_mock
    )

    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="medium"),
        make_mock_anthropic_message(text="ok"),
    ]

    await run(incoming=_make_incoming(), deps=deps)

    products_mock.assert_called_once()
    promos_mock.assert_called_once()


async def test_referential_message_with_history_uses_merged_classify_call(
    mock_anthropic: MagicMock, fake_redis: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit Batch B (B4): a referential message with existing history triggers
    ONE merged classify_and_condense Haiku call (not classify_intent followed
    by a separate condense_followup_query call)."""
    from datetime import UTC, datetime

    from polaris_api.services import pipeline
    from polaris_api.services.pipeline import PipelineDeps, run

    pool, conn = _make_pool_with_conn()
    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )
    incoming = _make_incoming(body="what about the KL one?")

    # Prior turns exist. save_message() write-throughs the just-saved user
    # turn into the Redis conversation cache (hot path), so load_history()
    # never reaches conn.fetch() here — pre-seed the SAME cache key with
    # two prior turns so window_pre picks them up, matching a real repeat
    # customer's conversation state.
    from polaris_api.services import conversation as conversation_service

    prior_messages = [
        conversation_service.ConversationMessage(
            role="user", content="Do you have a KL outlet?",
            created_at=datetime.now(UTC), token_count=6,
        ),
        conversation_service.ConversationMessage(
            role="assistant", content="Yes, we have one in KL.",
            created_at=datetime.now(UTC), token_count=7,
        ),
    ]
    await fake_redis.set(
        conversation_service._cache_key(incoming.tenant_id, pool._conv_id),  # noqa: SLF001
        conversation_service._serialize_messages(prior_messages),  # noqa: SLF001
        ex=86_400,
    )

    condense_mock = AsyncMock(return_value=("simple", "KL outlet opening hours"))
    classify_mock = AsyncMock()
    monkeypatch.setattr(pipeline.llm, "classify_and_condense", condense_mock)
    monkeypatch.setattr(pipeline.llm, "classify_intent", classify_mock)

    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="We're open 9-6.",
    )

    await run(incoming=incoming, deps=deps)

    condense_mock.assert_awaited_once()
    classify_mock.assert_not_called()
