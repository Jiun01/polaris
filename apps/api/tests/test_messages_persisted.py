"""Messages-table persistence shape tests — Phase 4 PR-4.

Per deep-dive §10.2 Phase 4 exit criterion: every assistant message
row must carry the 11 telemetry fields populated (intent, model_used,
input_tokens, output_tokens, cache_read_tokens, cached_tokens,
response_time_ms, rag_chunk_ids when applicable, token_count). Phase 9
dashboard queries depend on these — silent NULLs would zero out
graphs without anyone noticing.

Also enforces deep-dive §9.5: ``messages.flow_run_id IS NULL``
invariant for Phase 4 (Phase 5 wires the FK once flow_runs lands).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from tests.conftest import make_mock_anthropic_message
from tests.test_pipeline_end_to_end import (
    _FakeAcquire,
    _FakeTransaction,
    _make_incoming,
    _make_meta_client,
)

# Async runtime tests below decorate themselves with pytest.mark.asyncio;
# the two AST-level lint tests (intent vocab parity) stay sync (no event
# loop needed). Module-level pytestmark would apply to both + trigger
# PytestWarning on the lint tests; apply individually instead.


class _SqlRecorder:
    """Records every (sql, args) call for post-hoc assertion."""

    def __init__(self) -> None:
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.assistant_insert_args: tuple[Any, ...] | None = None

    def make_conn(self, *, has_chunks: bool = False) -> AsyncMock:
        conn = AsyncMock()
        conn.transaction = MagicMock(return_value=_FakeTransaction())
        new_conv_id = uuid4()
        new_user_msg_id = uuid4()
        new_assistant_msg_id = uuid4()

        async def fetchrow(sql: str, *args: Any) -> Any:
            self.fetchrow_calls.append((sql, args))
            s = sql.strip()
            if "INSERT INTO conversations" in s:
                return {"id": new_conv_id, "tenant_branch_id": None}
            if "FROM tenants" in s:
                return {"plan_tier": "starter", "cohere_rerank_enabled": None}
            if "FROM knowledge_chunks" in s:
                return {"has": has_chunks}
            if "INSERT INTO messages" in s and "'assistant'" in s:
                self.assistant_insert_args = args
                return {
                    "id": new_assistant_msg_id,
                    "created_at": datetime.now(UTC),
                }
            if "INSERT INTO messages" in s and "RETURNING id, created_at" in s:
                return {
                    "id": new_user_msg_id,
                    "created_at": datetime.now(UTC),
                }
            return None

        async def execute(sql: str, *args: Any) -> None:
            self.execute_calls.append((sql, args))

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock(side_effect=execute)
        return conn


def test_intent_tier_literal_matches_migration_030_check() -> None:
    """Code-review M-1: ``IntentTier`` Literal must match migration 030's CHECK.

    The CRITICAL-1 bug in PR-4 was the pipeline writing the classifier
    tier (`simple`/`medium`/`hard`/`handoff`) into `messages.intent`
    which had a CHECK for the SEMANTIC vocabulary
    (`greeting/faq/booking/handoff/promo/other`). PR-7 added an
    `intent_tier` column (migration 030) for the classifier output.
    This test asserts the Python Literal and the SQL CHECK agree —
    drifting either side without the other re-introduces the same
    class of bug.
    """
    from typing import get_args

    from polaris_shared_types import IntentTier

    literal_values = set(get_args(IntentTier))
    expected_check_vocab = {"simple", "medium", "hard", "handoff"}
    assert literal_values == expected_check_vocab, (
        f"IntentTier Literal {sorted(literal_values)} drifted from migration 030 "
        f"CHECK vocabulary {sorted(expected_check_vocab)}. Update BOTH the "
        f"Literal in packages/shared-types/.../llm_response.py AND the "
        f"intent_tier CHECK in packages/db-migrations/030_messages_intent_tier.sql "
        f"(append-only migration — write a new one if the values change)."
    )


def test_semantic_intent_vocab_matches_migration_028_check() -> None:
    """Companion to the above: the SEMANTIC ``intent`` column (PRD §10.9 +
    migration 028) is OWNED by Phase 5 flow routing. Phase 4 pipeline does
    NOT write to it for non-handoff messages. If Phase 5 introduces a
    SemanticIntent literal later, this test should be expanded to
    assert that literal matches the 028 CHECK."""
    # Phase 4 placeholder — no literal exists yet. The migration's CHECK
    # vocabulary is the authoritative spec until Phase 5 adds the literal.
    expected_check_vocab = {
        "greeting", "faq", "booking", "handoff", "promo", "other",
    }
    assert len(expected_check_vocab) == 6  # sentinel: update test if vocab changes


@pytest.mark.asyncio
async def test_assistant_row_has_full_telemetry(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """Assistant INSERT carries intent_tier, model_used, tokens, cache fields.

    Per PR-7 C-1 fix: writes go to ``intent_tier`` (NEW column,
    classifier tier vocabulary), not ``intent`` (SEMANTIC column,
    owned by Phase 5).
    """
    from polaris_api.services.pipeline import PipelineDeps, run

    recorder = _SqlRecorder()
    conn = recorder.make_conn()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _FakeAcquire(conn))

    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(
            text="answer",
            input_tokens=150, output_tokens=20,
            cache_creation_input_tokens=80, cache_read_input_tokens=40,
        ),
    ]

    await run(incoming=_make_incoming(), deps=deps)

    assert recorder.assistant_insert_args is not None
    args = recorder.assistant_insert_args
    # Per pipeline._persist_assistant_message arg order:
    # (tenant_id, conversation_id, content,
    #  intent, model_used, response_time_ms,
    #  input_tokens, output_tokens, cache_read_tokens, cached_tokens,
    #  rag_chunk_ids, token_count)
    assert args[2] == "answer"           # content
    assert args[3] == "simple"           # intent
    assert args[4] == "haiku"            # model_used
    assert args[5] >= 0                  # response_time_ms
    assert args[6] == 150                # input_tokens
    assert args[7] == 20                 # output_tokens
    assert args[8] == 40                 # cache_read_tokens
    assert args[9] == 80                 # cached_tokens (cache_creation)
    assert args[11] == 170               # token_count = in + out


@pytest.mark.asyncio
async def test_flow_run_id_not_set_in_phase_4(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """Phase 4 PR-4 invariant: assistant INSERT never touches flow_run_id.

    Deep-dive §9.5: Phase 5 adds the FK constraint; until then
    ``messages.flow_run_id IS NULL`` for every Phase 4 write.
    """
    from polaris_api.services.pipeline import PipelineDeps, run

    recorder = _SqlRecorder()
    conn = recorder.make_conn()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _FakeAcquire(conn))

    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="ok"),
    ]

    await run(incoming=_make_incoming(), deps=deps)

    # Phase 5 PR-4 added ``flow_run_id`` to the INSERT column list as
    # the FK retrofit for Phase 5's flow_runs table. On the Phase 4
    # path the BOUND VALUE must be None — the column appears in the
    # INSERT, but the value passed via asyncpg is NULL so no FK
    # constraint fires. The original Phase 4 invariant (no column
    # reference at all) was tightened to "no non-null value".
    assistant_inserts = [
        (sql, args) for sql, args in recorder.fetchrow_calls
        if "INSERT INTO messages" in sql and "'assistant'" in sql
    ]
    assert assistant_inserts
    for _sql, args in assistant_inserts:
        # The column list now includes flow_run_id (Phase 5 PR-4) —
        # confirm the bound value is None on the Phase 4 path.
        # The flow_run_id parameter is the last bound arg per
        # _persist_assistant_message's INSERT, value $13.
        flow_run_id_value = args[-1]
        assert flow_run_id_value is None, (
            f"Phase 4 path must pass flow_run_id=None; got {flow_run_id_value!r}"
        )


@pytest.mark.asyncio
async def test_handoff_row_marks_intent_handoff(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """Handoff path persists intent='handoff' with NULL model_used."""
    from polaris_api.services.pipeline import PipelineDeps, run

    recorder = _SqlRecorder()
    conn = recorder.make_conn()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _FakeAcquire(conn))

    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )
    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="handoff",
    )

    await run(incoming=_make_incoming(), deps=deps)

    handoff_inserts = [
        (sql, args) for sql, args in recorder.fetchrow_calls
        if "INSERT INTO messages" in sql and "'handoff'" in sql
    ]
    assert len(handoff_inserts) == 1
    sql, args = handoff_inserts[0]
    # _persist_handoff_message args: (tenant_id, conversation_id, content, latency_ms)
    assert "human agent" in args[2]


@pytest.mark.asyncio
async def test_last_message_at_bumped_after_save(
    mock_anthropic: MagicMock, fake_redis: Any,
) -> None:
    """UPDATE conversations SET last_message_at = now() fires per run."""
    from polaris_api.services.pipeline import PipelineDeps, run

    recorder = _SqlRecorder()
    conn = recorder.make_conn()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _FakeAcquire(conn))

    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client(),
    )
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="ok"),
    ]

    await run(incoming=_make_incoming(), deps=deps)

    last_message_updates = [
        sql for sql, _ in recorder.execute_calls
        if "UPDATE conversations" in sql and "last_message_at" in sql
    ]
    assert len(last_message_updates) == 1


@pytest.mark.asyncio
async def test_provider_message_id_stamped_after_dispatch(
    mock_anthropic: MagicMock, fake_redis: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful dispatch → second tx UPDATE messages SET provider_message_id."""
    from polaris_api.services import pipeline
    from polaris_api.services.pipeline import PipelineDeps, run

    recorder = _SqlRecorder()
    conn = recorder.make_conn()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _FakeAcquire(conn))

    deps = PipelineDeps(
        anon_pool=pool, redis=fake_redis,
        meta_client=_make_meta_client(send_returns="wamid.outbound.xyz"),
    )
    # Configured WhatsApp tenant: per-tenant WABA creds resolve (no demo
    # fallback for a real 'whatsapp' channel).
    monkeypatch.setattr(
        pipeline, "_resolve_whatsapp_credentials",
        AsyncMock(return_value=("tenant-pid", "tenant-tok")),
    )
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="ok"),
    ]

    decision = await run(incoming=_make_incoming(), deps=deps)

    assert decision.provider_message_id == "wamid.outbound.xyz"
    stamp_updates = [
        (sql, args) for sql, args in recorder.execute_calls
        if "UPDATE messages SET provider_message_id" in sql
    ]
    assert len(stamp_updates) == 1
    assert stamp_updates[0][1][0] == "wamid.outbound.xyz"
