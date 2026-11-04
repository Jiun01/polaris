"""Phase 3 exit-criteria — real-DB integration tests.

All four tests require a running Postgres instance with all migrations
applied and POLARIS_DATABASE_URL (or SUPABASE_DB_URL) set.  They skip
automatically without it.

Covered exit criteria:
  1. E2E flow — ingestion_request + knowledge_chunk → search_vector_only
     returns the seeded chunk (proves server-side schema + RPC wiring).
  2. Cross-tenant fail-loud — querying without SET LOCAL app.current_tenant_id
     returns zero rows; the RPC's belt-and-braces predicate prevents silent
     data leaks even when the GUC is omitted by the caller.
  3. Capacity gate override — tenant_billing.kb_mb_cap_override wins over
     tenant_quotas.knowledge_chunks_total_mb (AC-INGESTION-004 / ADR-032 Am).
  4. Branch-scoped RPC 4-way matrix — ADR-041 filter rule verified for all
     four (caller_branch, chunk_branch) combinations.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="function")

_PREFIX = "pytest-p3-e2e-"
# pgvector cosine distance of two zero vectors is NaN (0/0) — the
# KnowledgeChunkHit pydantic guard ``distance <= 2`` then rejects the
# result. Use a unit basis vector instead so the distance is always
# well-defined (cosine of identical unit vectors = 0; for any other
# pair = 1). The test contract is "does this chunk_id come back",
# not "does the vector ranking match expectations", so any
# distinct-but-defined vector is fine.
_UNIT_VEC = "[1.0," + ",".join(["0.0"] * 1023) + "]"
_FAKE_EMBED = [1.0] + [0.0] * 1023


def _dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest_asyncio.fixture(loop_scope="function")
async def db() -> AsyncIterator[asyncpg.Connection]:
    dsn = _dsn()
    if not dsn:
        pytest.skip("set POLARIS_DATABASE_URL to run Phase 3 real-DB tests")
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        try:
            await conn.execute(
                "DELETE FROM tenants WHERE slug LIKE $1",  # noqa: S608
                f"{_PREFIX}%",
            )
        except Exception:  # noqa: BLE001, S110 — best-effort test cleanup
            pass
        await conn.close()


async def _seed_tenant(
    conn: asyncpg.Connection,
    slug: str,
    *,
    kb_cap_mb: int = 100,
    kb_cap_override: int | None = None,
) -> uuid.UUID:
    tenant_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id",
        slug,
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
        ) VALUES (
            $1, 10000, 90, ARRAY['whatsapp'::text], false, false,
            $2, 60, 1000000, 1000000, 1000, 10000, 1000, 10000
        )
        """,
        tenant_id,
        kb_cap_mb,
    )
    if kb_cap_override is not None:
        await conn.execute(
            """
            INSERT INTO tenant_billing (tenant_id, plan_tier, kb_mb_cap_override)
            VALUES ($1, 'ultra', $2)
            """,
            tenant_id,
            kb_cap_override,
        )
    return tenant_id


async def _seed_chunk(
    conn: asyncpg.Connection,
    tenant_id: uuid.UUID,
    content: str,
    *,
    tenant_branch_id: uuid.UUID | None = None,
) -> uuid.UUID:
    request_id: uuid.UUID = await conn.fetchval(
        """
        INSERT INTO ingestion_requests (tenant_id, source_type, source_text, status)
        VALUES ($1, 'text', $2, 'completed')
        RETURNING id
        """,
        tenant_id,
        content,
    )
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    return await conn.fetchval(  # type: ignore[no-any-return]
        """
        INSERT INTO knowledge_chunks
            (tenant_id, tenant_branch_id, request_id, source_name,
             source_type, chunk_index, content, content_hash, embedding, token_count)
        VALUES ($1, $2, $3, 'test.md', 'text', 0, $4, $5, $6::vector, 10)
        RETURNING id
        """,
        tenant_id,
        tenant_branch_id,
        request_id,
        content,
        content_hash,
        _UNIT_VEC,
    )


# ---------------------------------------------------------------------------
# 1. E2E: ingestion_request → chunk → search returns it
# ---------------------------------------------------------------------------


async def test_e2e_submit_request_and_search_chunk(db: asyncpg.Connection) -> None:
    """Phase 3 exit criterion 1 — full ingestion-to-retrieval path.

    Simulates a desktop client completing an ingestion request and the
    subsequent RAG search returning the chunk that was written.
    """
    from polaris_api.services.knowledge_search import search_vector_only

    slug = f"{_PREFIX}e2e-{uuid.uuid4().hex[:6]}"
    tenant_id = await _seed_tenant(db, slug)

    content = f"e2e retrieval marker unique-{uuid.uuid4().hex}"
    chunk_id = await _seed_chunk(db, tenant_id, content)

    async with db.transaction():
        await db.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )
        hits = await search_vector_only(db, tenant_id, _FAKE_EMBED)

    assert any(h.id == chunk_id for h in hits), (
        f"chunk {chunk_id!s} not found; search returned {[str(h.id) for h in hits]}"
    )


# ---------------------------------------------------------------------------
# 2. Cross-tenant: missing SET LOCAL → zero rows (fail loud)
# ---------------------------------------------------------------------------


async def test_cross_tenant_no_set_local_returns_zero_rows(
    db: asyncpg.Connection,
) -> None:
    """Phase 3 exit criterion 2 — RPC belt-and-braces predicate.

    The RPC guards against callers that forget SET LOCAL by cross-checking
    the explicit tenant_id arg against current_setting('app.current_tenant_id').
    When the GUC is unset, current_setting returns NULL and the WHERE clause
    evaluates to FALSE for every row — zero results, no silent leak.
    """
    from polaris_api.services.knowledge_search import search_vector_only

    slug = f"{_PREFIX}ct-{uuid.uuid4().hex[:6]}"
    tenant_id = await _seed_tenant(db, slug)
    await _seed_chunk(db, tenant_id, f"cross-tenant marker {uuid.uuid4().hex}")

    # WITHOUT SET LOCAL: GUC is NULL → RPC predicate FALSE for all rows.
    async with db.transaction():
        hits_without_guc = await search_vector_only(db, tenant_id, _FAKE_EMBED)

    assert hits_without_guc == [], (
        "expected zero hits when GUC is unset (fail-loud behaviour); "
        f"got {len(hits_without_guc)} row(s) — potential silent data leak"
    )

    # WITH SET LOCAL: same query now returns the chunk (proves it was seeded).
    async with db.transaction():
        await db.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )
        hits_with_guc = await search_vector_only(db, tenant_id, _FAKE_EMBED)

    assert len(hits_with_guc) == 1, (
        f"expected 1 hit with GUC set, got {len(hits_with_guc)}"
    )


# ---------------------------------------------------------------------------
# 3. Capacity gate: tenant_billing.kb_mb_cap_override wins over tier cap
# ---------------------------------------------------------------------------


async def test_capacity_gate_respects_billing_override(
    db: asyncpg.Connection,
) -> None:
    """Phase 3 exit criterion 3 — AC-INGESTION-004 / ADR-032 Amendment.

    An Enterprise tenant with kb_mb_cap_override sees the override value.
    A Starter tenant without a billing row sees the tier default from quotas.
    """
    from polaris_api.services.knowledge_ingestion import get_effective_cap_mb

    enterprise_id = await _seed_tenant(
        db,
        f"{_PREFIX}ent-{uuid.uuid4().hex[:6]}",
        kb_cap_mb=100,
        kb_cap_override=10_000,
    )
    starter_id = await _seed_tenant(
        db,
        f"{_PREFIX}str-{uuid.uuid4().hex[:6]}",
        kb_cap_mb=100,
    )

    # get_effective_cap_mb uses a direct $1 arg (no GUC needed).
    enterprise_cap = await get_effective_cap_mb(db, enterprise_id)
    starter_cap = await get_effective_cap_mb(db, starter_id)

    assert enterprise_cap == 10_000, (
        f"expected Enterprise override of 10 000 MB; got {enterprise_cap}"
    )
    assert starter_cap == 100, (
        f"expected Starter tier cap of 100 MB; got {starter_cap}"
    )


# ---------------------------------------------------------------------------
# 4. Branch-scoped RPC: ADR-041 4-way matrix
# ---------------------------------------------------------------------------


async def test_branch_scoped_rpc_4way_matrix(db: asyncpg.Connection) -> None:
    """Phase 3 exit criterion 4 — ADR-041 branch filter.

    Filter rule in both RPCs:
      (p_tenant_branch_id IS NULL) OR
      (tenant_branch_id   IS NULL) OR
      (tenant_branch_id   = p_tenant_branch_id)

    4-way matrix tested (caller_branch × chunk_branch):
      NULL × NULL → included  (global caller, global chunk)
      NULL × X    → included  (global caller sees all branches)
      X    × NULL → included  (branch-X caller sees global chunks)
      X    × Y    → EXCLUDED  (branch mismatch)
    """
    from polaris_api.services.knowledge_search import search_vector_only

    slug = f"{_PREFIX}br-{uuid.uuid4().hex[:6]}"
    tenant_id = await _seed_tenant(db, slug)

    # ``knowledge_chunks.tenant_branch_id`` FKs ``tenant_branches.id``
    # (migration 017a + 020_knowledge_chunks). Seed real rows instead
    # of using random UUIDs or the insert into knowledge_chunks raises
    # ``knowledge_chunks_tenant_branch_id_fkey``. The branch names are
    # arbitrary; the test only cares about the filter rule.
    branch_x = await db.fetchval(
        "INSERT INTO tenant_branches (tenant_id, name) VALUES ($1, $2) RETURNING id",
        tenant_id, f"pytest-branch-x-{uuid.uuid4().hex[:6]}",
    )
    branch_y = await db.fetchval(
        "INSERT INTO tenant_branches (tenant_id, name) VALUES ($1, $2) RETURNING id",
        tenant_id, f"pytest-branch-y-{uuid.uuid4().hex[:6]}",
    )

    chunk_null_id = await _seed_chunk(
        db, tenant_id, f"global chunk {uuid.uuid4().hex}"
    )
    chunk_x_id = await _seed_chunk(
        db, tenant_id, f"branch-x chunk {uuid.uuid4().hex}", tenant_branch_id=branch_x
    )
    chunk_y_id = await _seed_chunk(
        db, tenant_id, f"branch-y chunk {uuid.uuid4().hex}", tenant_branch_id=branch_y
    )

    async def _search(caller_branch: uuid.UUID | None) -> set[uuid.UUID]:
        async with db.transaction():
            await db.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )
            hits = await search_vector_only(
                db, tenant_id, _FAKE_EMBED, tenant_branch_id=caller_branch
            )
        return {h.id for h in hits}

    # NULL caller → global view; all 3 chunks visible.
    all_ids = await _search(None)
    assert chunk_null_id in all_ids, "global chunk missing from NULL-caller result"
    assert chunk_x_id in all_ids, "branch-x chunk missing from NULL-caller result"
    assert chunk_y_id in all_ids, "branch-y chunk missing from NULL-caller result"

    # branch_x caller → sees global + branch_x, NOT branch_y.
    x_caller_ids = await _search(branch_x)
    assert chunk_null_id in x_caller_ids, "global chunk must be visible to branch-x caller"
    assert chunk_x_id in x_caller_ids, "branch-x chunk must be visible to branch-x caller"
    assert chunk_y_id not in x_caller_ids, (
        "branch-y chunk must NOT be visible to branch-x caller (ADR-041 mismatch)"
    )
