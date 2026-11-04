"""Shared pytest fixtures for apps/api tests.

Concerns living here:

1. `client` — ASGI-bound httpx client for route tests (Phase 0; used by test_health.py).
2. `service_dsn` — session-scoped: resolves the DSN, ensures `auth.users` shim +
   `polaris_test_anon` role + grants exist. Skips the whole RLS test class if no
   DB is reachable. Idempotent — safe to re-run.
3. `service_conn` — function-scoped: yields an `asyncpg.Connection`. Cleans up
   rows it inserted via a `pytest-` slug prefix convention.
4. `fake_redis` (Phase 2 PR-1+) — function-scoped in-memory Redis double for
   webhook idempotency + queue tests. Avoids needing a live Redis to run
   `make verify` in isolated CI.

PR-1 design note: tests can't use PR-2's real `polaris_anon` role yet (it doesn't
exist until migration 010 lands). Instead, the fixture creates an ephemeral
`polaris_test_anon` role with `NOBYPASSRLS` so `SET LOCAL ROLE polaris_test_anon`
exercises RLS exactly as production will. Rows are seeded as the service-role
user (BYPASSRLS) and read back through the anon role to prove invisibility.

Known issue — function-scoped admin test fixtures + session event loop
---------------------------------------------------------------------
Test files that build their own AsyncClient against the main `app` AND
override async deps via `app.dependency_overrides` (currently
`test_admin_chunking_config.py` + `test_admin_force_run.py`) must pin
``loop_scope="function"`` on both `pytestmark` and the async fixture.
Without the pin, function-scope teardown closes the session event loop
that `service_dsn` (session-scope, async) holds, surfacing as
"RuntimeError: There is no current event loop in thread 'MainThread'"
in every test that runs after the offender. The root cause inside
pytest-asyncio hasn't been isolated yet (review M6, PR-5); the pin is
the working remediation.

If a future admin test file lands without this pin and the full suite
starts failing in test files alphabetically after the new one, this is
why.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import fakeredis.aioredis
import pytest
import pytest_asyncio
import redis.asyncio as redis_async
from httpx import ASGITransport, AsyncClient

# Test JWT env defaults — MUST be set before importing polaris_api.main, which
# calls get_settings() at module import and caches it. `uv run pytest` does not
# export the repo .env, but pydantic-settings still reads the .env FILE; without
# these, the app caches the .env placeholder JWT_SECRET and every minted-JWT auth
# test 401s locally (CI sets these as real env vars, so it was green there).
# setdefault() keeps any real env var authoritative. KMS is intentionally NOT set
# here so DB-integration tests keep decrypting with the real .env key.
os.environ.setdefault("JWT_SECRET", "pytest-secret-32-bytes-padded-xxxxxxxxxx")
os.environ.setdefault("JWT_ISSUER", "polaris-portal")

from polaris_api.main import app  # noqa: E402 — import after test-env defaults above

# Tenant slug prefix used for cleanup. Tests that touch tenants/users/channels
# must use slugs starting with this prefix so the per-test cleanup hook can
# safely delete them.
_TEST_TENANT_PREFIX = "pytest-"
_TEST_USER_EMAIL_SUFFIX = "@pytest.invalid"
_TEST_ANON_ROLE = "polaris_test_anon"


def _dsn_from_env() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """ASGI-bound httpx client — no socket, runs the app in-process."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[redis_async.Redis]:
    """In-memory Redis double for Phase 2+ webhook tests.

    Returns a `fakeredis.aioredis.FakeRedis` which speaks the same
    `redis.asyncio.Redis` protocol — same return types for `set(... nx=True)`
    (True on first-write, None on conflict), same TTL semantics. Cleared at
    fixture teardown so test isolation is guaranteed.

    Used directly by unit tests of `services/idempotency.py` (PR-4). Routes
    that go through `get_redis()` are tested via the `monkeypatch` approach
    in the per-test setup — `app.dependency_overrides` isn't enough because
    the router imports the singleton accessor at call time, not as a Depends.
    """
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest_asyncio.fixture(scope="session")
async def service_dsn() -> AsyncIterator[str]:
    """Resolve DSN once per test session and ensure the RLS-test prerequisites
    are in place. Skips the entire RLS test class if no DB is reachable.

    Prerequisites (idempotent):
    - `auth.users` table — minimal shim for FK target. Supabase ships this
      natively with a rich schema; the IF NOT EXISTS guard means we only seed
      it on bare local Postgres.
    - `polaris_test_anon` role — NOBYPASSRLS, used by RLS tests via
      `SET LOCAL ROLE`. Distinct from PR-2's `polaris_anon` so it's clearly
      a test artefact.
    - Grants on every Phase 1 table to the test role (USAGE on schema, full
      DML on tables). RLS is then the only gate — exactly the production
      posture.
    """
    dsn = _dsn_from_env()
    if not dsn:
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL to run RLS tests")

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB unreachable: {type(exc).__name__}")

    try:
        # auth.users shim — no-op on Supabase, seeds on local.
        await conn.execute(
            """
            CREATE SCHEMA IF NOT EXISTS auth;
            CREATE TABLE IF NOT EXISTS auth.users (
              id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              email TEXT UNIQUE
            );
            """
        )

        # polaris_test_anon role + grants. CREATE ROLE is non-transactional; the
        # DO block makes it idempotent. Grants are issued unconditionally — they
        # may target tables that don't exist yet on a fresh DB; the IF NOT EXISTS
        # in subsequent ALTER TABLE … OWNER ... wouldn't apply, so we wrap each
        # GRANT in its own try-block via a DO/EXCEPTION pattern.
        await conn.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_TEST_ANON_ROLE}') THEN
                CREATE ROLE {_TEST_ANON_ROLE} NOLOGIN NOBYPASSRLS;
              END IF;
            END $$;
            """  # noqa: S608 — role name is a constant literal, not user input
        )
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {_TEST_ANON_ROLE}")
        await conn.execute(f"GRANT USAGE ON SCHEMA auth TO {_TEST_ANON_ROLE}")

        # Per-table grants for tables consumed by RLS-isolation tests under
        # the polaris_test_anon role. Wrapped in a DO so a table missing
        # (e.g. PR-1 runs before its phase migration applies) does not
        # abort the fixture setup.
        #
        # Phase 1 baseline: tenants, tenant_users, tenant_channels.
        # Phase 8 PR-1 additions: notifications, plan_change_requests
        #   (migration 051) — without these grants test_tenant_notifications
        #   hits 'permission denied for table notifications' under
        #   polaris_test_anon. See PR #34 CI failure on 2026-05-20.
        # Phase 8 PR-6 additions: conversations, messages so the
        #   read-only Conversations viewer RLS tests can exercise SELECT
        #   under the test anon role.
        # Phase 8 PR-7 additions: flow_definitions + flow_templates so
        #   the flow CRUD + template picker tests can exercise SELECT /
        #   INSERT / UPDATE under polaris_test_anon. flow_templates is
        #   platform-scoped (no RLS) so SELECT alone is sufficient.
        for table in (
            "tenants",
            "tenant_users",
            "tenant_channels",
            "notifications",
            "plan_change_requests",
            "conversations",
            "messages",
            "flow_definitions",
            "flow_templates",
        ):
            await conn.execute(
                f"""
                DO $$
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                  ) THEN
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO {_TEST_ANON_ROLE}';
                  END IF;
                END $$;
                """  # noqa: S608
            )
        # Need SELECT on auth.users for the FK + RLS read paths.
        await conn.execute(f"GRANT SELECT ON auth.users TO {_TEST_ANON_ROLE}")
    finally:
        await conn.close()

    yield dsn


# ------------------------------------------------------------------
# Phase 4 PR-1: LLM client mocks
# ------------------------------------------------------------------
# Shared fixtures so the test_llm_*.py files don't each rebuild the
# same MagicMock scaffolding. Two factories at module-scope keep the
# Anthropic + OpenRouter SDK shape in one place — if the SDK changes
# its usage field names, fix it here once, not in five test files.


def make_mock_anthropic_message(
    *,
    text: str = "ok",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> MagicMock:
    """Build a MagicMock that quacks like ``anthropic.types.Message``.

    Used for ``messages.create`` returns and as the
    ``stream.get_final_message()`` return value. The two cache fields
    are the load-bearing ones for Phase 4 exit criterion 1 (verify
    cache hit rate > 0 on second message).
    """
    msg = MagicMock()
    text_block = MagicMock()
    text_block.text = text
    msg.content = [text_block]
    msg.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    return msg


class FakeAnthropicStream:
    """Async context manager mimicking ``client.messages.stream(...)``.

    Yields the provided text chunks through ``text_stream``, then
    surfaces the final ``Message`` via ``get_final_message()``. Tests
    that need a stream-failure path can pass ``raise_in_iter`` to
    have the async iterator raise mid-stream.
    """

    def __init__(
        self,
        chunks: list[str],
        final: MagicMock,
        raise_in_iter: BaseException | None = None,
    ) -> None:
        self._chunks = chunks
        self._final = final
        self._raise = raise_in_iter

    async def __aenter__(self) -> FakeAnthropicStream:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    @property
    def text_stream(self) -> Any:
        chunks = self._chunks
        raise_in_iter = self._raise

        async def _gen() -> Any:
            for c in chunks:
                yield c
            if raise_in_iter is not None:
                raise raise_in_iter

        return _gen()

    async def get_final_message(self) -> MagicMock:
        return self._final


@pytest.fixture
def mock_anthropic(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a mock ``AsyncAnthropic`` client and reset cache.

    Returns the MagicMock so tests can assert on
    ``mock_anthropic.messages.create.call_args`` (etc.). The
    ``messages.create`` mock returns a default ``ok`` reply with
    100 input / 20 output tokens; tests override
    ``mock_anthropic.messages.create.return_value`` to customize.
    """
    from polaris_api.services import llm as llm_module

    mock = MagicMock()
    mock.messages.create = AsyncMock(
        return_value=make_mock_anthropic_message(text="ok")
    )
    # Default stream returns one chunk + matching final message.
    default_final = make_mock_anthropic_message(text="streamed ok")
    mock.messages.stream = MagicMock(
        return_value=FakeAnthropicStream(
            chunks=["streamed ", "ok"], final=default_final,
        )
    )

    llm_module._reset_clients_for_testing()
    monkeypatch.setattr(llm_module, "_get_anthropic_client", lambda: mock)
    return mock


# ------------------------------------------------------------------
# Phase 4 PR-2: RAG / Cohere mocks
# ------------------------------------------------------------------


def make_mock_chunk(
    *,
    content: str = "fact",
    source_name: str = "faq.md",
    rrf_score: float = 0.9,
) -> Any:
    """Build a KnowledgeChunkHit for service-level RAG tests.

    Lives here so the 3 test_rag_*.py files don't each rebuild the
    same Pydantic shape. uuid4 surfaces deterministically through
    the chunk's ``.id`` field for assertion stability.
    """
    from uuid import uuid4

    from polaris_shared_types import KnowledgeChunkHit
    return KnowledgeChunkHit(
        id=uuid4(),
        content=content,
        source_name=source_name,
        rrf_score=rrf_score,
        vector_rank=1,
        bm25_rank=1,
        distance=None,
    )


def make_mock_cohere_rerank_response(
    *,
    scores: list[float],
) -> MagicMock:
    """Build a cohere rerank response with N results, one per score.

    ``scores`` ordering corresponds to ranked position. Index field
    on each result references the candidate's original list position
    — tests set it explicitly to control which chunk gets reordered.
    """
    response = MagicMock()
    response.results = []
    for idx, score in enumerate(scores):
        result = MagicMock()
        result.index = idx
        result.relevance_score = score
        response.results.append(result)
    return response


@pytest.fixture
def mock_cohere(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a mock cohere AsyncClient and reset the cache.

    Default rerank returns three results, all above 0.3 threshold
    (0.9, 0.7, 0.5). Tests override
    ``mock_cohere.rerank.return_value`` to inject other shapes.
    """
    from polaris_api.services import rag as rag_module

    mock = MagicMock()
    mock.rerank = AsyncMock(
        return_value=make_mock_cohere_rerank_response(
            scores=[0.9, 0.7, 0.5],
        )
    )
    rag_module._reset_clients_for_testing()
    monkeypatch.setattr(rag_module, "_get_cohere_client", lambda: mock)
    return mock


def make_mock_openai_completion(
    *,
    text: str = "fallback ok",
    prompt_tokens: int = 150,
    completion_tokens: int = 30,
) -> MagicMock:
    """Build a MagicMock that quacks like ``openai.types.ChatCompletion``."""
    completion = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    completion.choices = [choice]
    completion.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return completion


@pytest.fixture
def mock_openrouter(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a mock OpenRouter (AsyncOpenAI) client and reset cache."""
    from polaris_api.services import llm as llm_module

    mock = MagicMock()
    mock.chat.completions.create = AsyncMock(
        return_value=make_mock_openai_completion()
    )

    llm_module._reset_clients_for_testing()
    monkeypatch.setattr(llm_module, "_get_openrouter_client", lambda: mock)
    return mock


@pytest_asyncio.fixture
async def service_conn(service_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    """Yield an asyncpg.Connection running as the service role (BYPASSRLS). Used
    by tests to seed rows and to drive role-switching for RLS assertions.

    Cleanup: drop every test-owned row by slug/email prefix. Best-effort — if a
    table doesn't exist yet (early-PR runs against partial schema), the DELETE
    is wrapped in IF EXISTS guards via information_schema.
    """
    conn = await asyncpg.connect(service_dsn)
    try:
        yield conn
    finally:
        try:
            # Order: children before parents (FK constraints).
            for table in ("tenant_channels", "tenant_users"):
                await conn.execute(
                    f"""
                    DO $$
                    BEGIN
                      IF EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = '{table}'
                      ) THEN
                        EXECUTE 'DELETE FROM public.{table} WHERE tenant_id IN '
                          || '(SELECT id FROM public.tenants WHERE slug LIKE ''{_TEST_TENANT_PREFIX}%'')';
                      END IF;
                    END $$;
                    """  # noqa: S608
                )
            await conn.execute(
                f"""
                DO $$
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'tenants'
                  ) THEN
                    EXECUTE 'DELETE FROM public.tenants WHERE slug LIKE ''{_TEST_TENANT_PREFIX}%''';
                  END IF;
                END $$;
                """  # noqa: S608
            )
            await conn.execute(
                f"DELETE FROM auth.users WHERE email LIKE '%{_TEST_USER_EMAIL_SUFFIX}'"  # noqa: S608
            )
        except Exception:  # noqa: BLE001, S110 — cleanup is best-effort; a failure here must not mask the real test failure
            pass
        await conn.close()
