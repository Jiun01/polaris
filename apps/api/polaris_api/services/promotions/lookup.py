"""Active-promotions lookup for the bot reply path — QA report F7.

``tenant_promotions`` (migration 053) was designed for a bot-side lookup
(the migration even ships the partial index ``tenant_promotions_tenant_active_idx``
and documents the filter ``is_active AND now() BETWEEN valid_from AND valid_until``),
but nothing in ``services/`` ever queried it — promotions reached customers only
if separately copied into a knowledge chunk. This module is that missing read.

Assumes the caller opened a connection with ``app.current_tenant_id`` set (the
pipeline's anon-pool TenantContext); RLS strips other tenants' rows and the
explicit ``tenant_id`` predicate is belt-and-braces (mirrors knowledge_search).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import asyncpg

# Defensive cap — a bot reply should never dump a huge promo list into the LLM
# context. A tenant with more than a handful of simultaneously-active promos is
# unusual; we take the most recently-updated few.
_MAX_ACTIVE_PROMOS: Final[int] = 5


@dataclass(slots=True)
class ActivePromotion:
    """One currently-valid promotion, trimmed to the bot-facing fields."""

    id: UUID
    title: str
    description: str
    discount_text: str
    terms: str | None


async def fetch_active_promotions(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    *,
    tenant_branch_id: UUID | None = None,
) -> list[ActivePromotion]:
    """Return the tenant's currently-valid promotions (empty list if none).

    "Currently valid" = ``is_active`` AND ``now()`` within
    ``[valid_from, valid_until]`` (the filter migration 053 anticipated).
    Branch-scoped per ADR-041: a NULL ``tenant_branch_id`` on the row is shared
    across branches; a branch-bound row only surfaces for that branch (or when
    the caller passes no branch filter).
    """
    rows = await conn.fetch(
        """
        SELECT id, title, description, discount_text, terms
          FROM tenant_promotions
         WHERE tenant_id = $1
           AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
           AND is_active
           AND now() BETWEEN valid_from AND valid_until
           AND ($2::uuid IS NULL OR tenant_branch_id IS NULL OR tenant_branch_id = $2)
         ORDER BY updated_at DESC
         LIMIT $3
        """,
        tenant_id,
        tenant_branch_id,
        _MAX_ACTIVE_PROMOS,
    )
    return [
        ActivePromotion(
            id=r["id"],
            title=r["title"],
            description=r["description"],
            discount_text=r["discount_text"],
            terms=r["terms"],
        )
        for r in rows
    ]
