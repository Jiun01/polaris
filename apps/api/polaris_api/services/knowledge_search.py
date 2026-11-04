"""Knowledge-search service — Phase 3 PR-2.

Thin Python wrapper over the two search RPCs in migration 024.
Phase 4 RAG imports this module (NOT the raw RPC) so the calling
shape stays stable as the SQL evolves.

Both functions assume the caller has already opened a transaction
on an anon-pool connection with ``SET LOCAL app.current_tenant_id``
applied (i.e. they're invoked under a ``TenantContext`` Depends
chain). RLS strips other tenants' rows even if the explicit
tenant_id argument were spoofed; the RPC's own
``tenant_id = current_setting(...)`` predicate is belt-and-braces
that surfaces a forgotten SET LOCAL as a fail-loud zero-row result
rather than a silent leak.

The hybrid path tries ``websearch_to_tsquery`` first, falls back to
``plainto_tsquery`` if that parses cleanly, else degrades to the
vector-only RPC. ``websearch_to_tsquery`` is operator-friendly
(quotes, OR, leading `-`) but raises ``SyntaxError`` on a handful
of inputs the operator parser can't handle — falling back keeps
search responsive instead of bubbling a 500 to the caller.
"""

from __future__ import annotations

import logging
import math
from typing import Final
from uuid import UUID

import asyncpg
from polaris_shared_types import KnowledgeChunkHit


def _finite_or_none(val: float | None) -> float | None:
    """Coerce a DB cosine ``distance`` to a finite float, else ``None``.

    pgvector cosine distance is ``NaN`` when either side is a zero-norm vector —
    which happens whenever ``embed_query`` returns its zero-vector fallback (an
    OpenRouter embedding outage, a placeholder key, or empty text). A raw
    ``float('nan')`` then fails ``KnowledgeChunkHit.distance``'s ``<= 2`` bound and
    500s the whole reply. Treat non-finite as "distance unknown" (``None``), the
    same as a BM25-only hit the vector leg never scored — so the reply still ships
    on the lexical leg instead of crashing.
    """
    if val is None:
        return None
    f = float(val)
    return f if math.isfinite(f) else None

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion constant — Cormack et al. 2009 recommend k=60
# as a robust default across IR benchmarks. Per ADR-032 Amendment +
# Q-PHASE3-04 (locked); revisit if Phase 4 eval shows recall pain.
_RRF_K: Final[int] = 60

# Default candidate count to return from each RPC. Phase 4 RAG will
# override per-tenant if needed (Enterprise plans may want larger
# top_k for richer reranker input).
_DEFAULT_TOP_K: Final[int] = 5


def _vec_literal(embedding: list[float]) -> str:
    """Format a 1024-dim list as a pgvector string literal '[x,y,z]'.

    asyncpg doesn't bind Python lists to the pgvector ``vector`` type
    out of the box (would need ``pgvector.asyncpg.register_vector(conn)``
    at every pool acquire). The string literal + ``::vector`` cast at
    the SQL site is dep-free and works on every connection. Negligible
    overhead for 1024 floats.

    Non-finite values (NaN/+-Inf) — which a misbehaving embedding provider can
    return — are coerced to 0.0 so the ``::vector`` cast never rejects the literal
    and crashes the request mid-pipeline.
    """
    import math  # noqa: PLC0415

    return "[" + ",".join(
        repr(f if math.isfinite(f := float(v)) else 0.0) for v in embedding
    ) + "]"


async def search_hybrid(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    query_text: str,
    query_embedding: list[float],
    *,
    tenant_branch_id: UUID | None = None,
    top_k: int = _DEFAULT_TOP_K,
) -> list[KnowledgeChunkHit]:
    """Hybrid BM25 + vector search via ``match_knowledge_chunks_hybrid``.

    Falls back to vector-only on ``websearch_to_tsquery`` parse errors
    (asyncpg raises ``asyncpg.PostgresSyntaxError`` for parser
    failures inside SQL functions). The fallback synthesises an
    ``rrf_score = 1.0 - distance`` so Phase 4 RAG can sort the
    response set uniformly.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT id, content, source_name, rrf_score, vector_rank, bm25_rank,
                   vector_distance
              FROM match_knowledge_chunks_hybrid($1, $2::vector, $3, $4, $5, $6)
            """,
            query_text,
            _vec_literal(query_embedding),
            tenant_id,
            tenant_branch_id,
            top_k,
            _RRF_K,
        )
    except asyncpg.PostgresError as exc:
        # `websearch_to_tsquery` raises on a handful of operator-parser
        # edge cases (unbalanced quotes, leading `-`, undefined text-
        # search config, etc.). PR-5 (review M3) broadens the catch
        # from PostgresSyntaxError-only to the full PostgresError tree
        # so the hybrid path degrades to vector-only instead of 500ing
        # on any of those variants. The class name is logged so we
        # still notice if a new asyncpg error class starts surfacing
        # from this path (perf regression hidden behind the fallback).
        logger.info(
            "search_hybrid degraded to vector-only for tenant_id=%s: %s",
            tenant_id, type(exc).__name__,
        )
        return await search_vector_only(
            conn, tenant_id, query_embedding,
            tenant_branch_id=tenant_branch_id, top_k=top_k,
        )
    return [
        KnowledgeChunkHit(
            id=row["id"],
            content=row["content"],
            source_name=row["source_name"],
            rrf_score=float(row["rrf_score"]),
            vector_rank=row["vector_rank"],
            bm25_rank=row["bm25_rank"],
            # F2 (migration 113): raw cosine distance from the vector leg
            # (lower = closer). NULL for a BM25-only hit the vector leg missed;
            # None too for a non-finite (NaN) distance from a zero query vector.
            distance=_finite_or_none(row["vector_distance"]),
        )
        for row in rows
    ]


async def search_vector_only(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    query_embedding: list[float],
    *,
    tenant_branch_id: UUID | None = None,
    top_k: int = _DEFAULT_TOP_K,
) -> list[KnowledgeChunkHit]:
    """Vector-only fallback via ``match_knowledge_chunks``.

    Used directly for emoji-only / non-text queries and as the
    fallback path from ``search_hybrid``. The synthesised
    ``rrf_score`` is ``1.0 - distance`` so sorting parity with the
    hybrid path holds.
    """
    rows = await conn.fetch(
        """
        SELECT id, content, source_name, distance
          FROM match_knowledge_chunks($1::vector, $2, $3, $4)
        """,
        _vec_literal(query_embedding),
        tenant_id,
        tenant_branch_id,
        top_k,
    )
    return [
        KnowledgeChunkHit(
            id=row["id"],
            content=row["content"],
            source_name=row["source_name"],
            # Walrus binds the finite-guarded distance once; reused below. A
            # non-finite distance (zero query vector) → score 0.0 + distance None.
            rrf_score=(1.0 - d) if (d := _finite_or_none(row["distance"])) is not None else 0.0,
            vector_rank=None,
            bm25_rank=None,
            distance=d,
        )
        for row in rows
    ]
