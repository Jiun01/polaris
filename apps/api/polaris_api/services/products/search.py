"""Product retrieval service — Phase 15 PR-4.

Thin Python wrapper over the two product-search RPCs in migration 090
(``match_products_hybrid`` / ``match_products``). Mirrors
``services/knowledge_search.py`` exactly so the bot's product lookup behaves
like KB retrieval: hybrid BM25 + vector + RRF, with a vector-only fallback when
``websearch_to_tsquery`` can't parse the query.

Both functions assume the caller opened a transaction on an anon-pool
connection with ``SET LOCAL app.current_tenant_id`` applied (TenantContext
Depends chain / pipeline). RLS strips other tenants' rows; the RPC's own
``tenant_id = current_setting(...)`` predicate is belt-and-braces. Only
``is_active`` products are searchable (enforced in the RPC).
"""

from __future__ import annotations

import logging
from typing import Final
from uuid import UUID

import asyncpg
from polaris_shared_types import ProductHit

from polaris_api.services.embed import to_pgvector_literal

logger = logging.getLogger(__name__)

# RRF constant — match knowledge_search (Cormack et al. 2009, k=60).
_RRF_K: Final[int] = 60
_DEFAULT_TOP_K: Final[int] = 5


async def search_products_hybrid(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    query_text: str,
    query_embedding: list[float],
    *,
    tenant_branch_id: UUID | None = None,
    top_k: int = _DEFAULT_TOP_K,
) -> list[ProductHit]:
    """Hybrid BM25 + vector product search via ``match_products_hybrid``.

    Degrades to vector-only on a ``websearch_to_tsquery`` parse error (same
    failure modes as KB hybrid search) so the bot still gets product matches.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT id, name, description, price_text, rrf_score,
                   vector_rank, bm25_rank, vector_distance
              FROM match_products_hybrid($1, $2::vector, $3, $4, $5, $6)
            """,
            query_text,
            to_pgvector_literal(query_embedding),
            tenant_id,
            tenant_branch_id,
            top_k,
            _RRF_K,
        )
    except asyncpg.PostgresError as exc:
        logger.info(
            "search_products_hybrid degraded to vector-only for tenant_id=%s: %s",
            tenant_id, type(exc).__name__,
        )
        return await search_products_vector_only(
            conn, tenant_id, query_embedding,
            tenant_branch_id=tenant_branch_id, top_k=top_k,
        )
    return [
        ProductHit(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            price_text=row["price_text"],
            rrf_score=float(row["rrf_score"]),
            vector_rank=row["vector_rank"],
            bm25_rank=row["bm25_rank"],
            # F2 (migration 113): cosine distance from the vector leg; NULL for a
            # BM25-only hit the vector leg missed.
            distance=(
                float(row["vector_distance"])
                if row["vector_distance"] is not None
                else None
            ),
        )
        for row in rows
    ]


async def search_products_vector_only(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    query_embedding: list[float],
    *,
    tenant_branch_id: UUID | None = None,
    top_k: int = _DEFAULT_TOP_K,
) -> list[ProductHit]:
    """Vector-only fallback via ``match_products``. Synthesises
    ``rrf_score = 1.0 - distance`` for sort parity with the hybrid path."""
    rows = await conn.fetch(
        """
        SELECT id, name, description, price_text, distance
          FROM match_products($1::vector, $2, $3, $4)
        """,
        to_pgvector_literal(query_embedding),
        tenant_id,
        tenant_branch_id,
        top_k,
    )
    return [
        ProductHit(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            price_text=row["price_text"],
            rrf_score=float(1.0 - row["distance"]),
            vector_rank=None,
            bm25_rank=None,
            distance=float(row["distance"]),
        )
        for row in rows
    ]


async def fetch_images_for(
    conn: asyncpg.Connection, product_ids: list[UUID]
) -> dict[UUID, list[str]]:
    """Return ordered storage paths per product, for the SKUs the bot chose to
    show (PR-6 mints signed read URLs from these). Ordered by gallery position.
    """
    if not product_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT product_id, storage_path
        FROM tenant_product_images
        WHERE product_id = ANY($1::uuid[])
        ORDER BY product_id, sort_order, created_at
        """,
        product_ids,
    )
    grouped: dict[UUID, list[str]] = {pid: [] for pid in product_ids}
    for r in rows:
        grouped[r["product_id"]].append(r["storage_path"])
    return grouped
