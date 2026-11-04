"""Knowledge-gap aggregation — M2-lite (Knowledge Gap Detection / M3 backing).

Turns raw ``knowledge_gap_misses`` rows into ranked themes for the dashboard,
grouping by NORMALIZED question text (lowercase, punctuation→space, whitespace
collapsed) entirely in SQL — no embeddings yet. The semantic-clustering upgrade
(M2 proper) swaps the grouping key without changing this module's signature.

Read-only; runs under the portal request's tenant-GUC connection, so RLS scopes
every row to the calling tenant (the explicit ``tenant_id = $1`` filter is
defence-in-depth alongside the policy).
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from polaris_shared_types.knowledge_gaps import KnowledgeGapExample, KnowledgeGapTheme

# Normalize: non-alphanumeric runs → space, lowercase, collapse whitespace, trim.
# Groups "Refund?" / "refund" / "  REFUND " into one theme. example_question is
# the most-recent RAW phrasing so the tenant sees real customer wording.
# norm_q is SELECTed and returned so the frontend has a stable key to pass back
# to the /summary/examples drill-down endpoint.
_THEMES_SQL = """
WITH norm AS (
    SELECT
        btrim(
            regexp_replace(
                lower(regexp_replace(customer_question, '[^[:alnum:][:space:]]+', ' ', 'g')),
                '\\s+', ' ', 'g'
            )
        ) AS norm_q,
        customer_question,
        signal_type,
        created_at
    FROM knowledge_gap_misses
    WHERE tenant_id = $1
      AND created_at >= now() - make_interval(days => $2)
)
SELECT
    norm_q,
    (array_agg(customer_question ORDER BY created_at DESC))[1]            AS example_question,
    count(*)::int                                                        AS ask_count,
    count(*) FILTER (WHERE created_at >= now() - interval '7 days')::int  AS recent_count,
    max(created_at)                                                      AS last_seen,
    count(*) FILTER (WHERE signal_type = 'handoff')::int                 AS handoff_count,
    count(*) FILTER (WHERE signal_type = 'no_retrieval')::int            AS no_retrieval_count,
    count(*) FILTER (WHERE signal_type = 'low_retrieval')::int           AS low_retrieval_count
FROM norm
WHERE norm_q <> ''
GROUP BY norm_q
ORDER BY ask_count DESC, last_seen DESC
LIMIT $3
"""

_TOTAL_SQL = """
SELECT count(*)::int AS total
  FROM knowledge_gap_misses
 WHERE tenant_id = $1
   AND created_at >= now() - make_interval(days => $2)
"""

# Examples drill-down: rows whose NORMALIZED question equals the passed norm_q.
# Reuses the EXACT same normalization expression as _THEMES_SQL so matches are
# guaranteed to be consistent with what the grouping produced.
_EXAMPLES_SQL = """
SELECT
    conversation_id::text,
    created_at,
    customer_question,
    signal_type
FROM knowledge_gap_misses
WHERE tenant_id = $1
  AND created_at >= now() - make_interval(days => $2)
  AND btrim(
        regexp_replace(
            lower(regexp_replace(customer_question, '[^[:alnum:][:space:]]+', ' ', 'g')),
            '\\s+', ' ', 'g'
        )
      ) = $3
ORDER BY created_at DESC
LIMIT $4
"""


async def summarize_gaps(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    window_days: int,
    limit: int,
) -> tuple[int, list[KnowledgeGapTheme]]:
    """Return ``(total_misses_in_window, ranked_themes)`` for the tenant.

    ``themes`` is ordered highest ask_count first and capped at ``limit``.
    ``total_misses`` counts the pre-grouping rows in the window (so the UI can
    show "N misses across M themes").  Each theme includes ``norm_q`` — the
    stable normalized key the frontend passes to the /summary/examples endpoint.
    """
    total = await conn.fetchval(_TOTAL_SQL, tenant_id, window_days) or 0
    rows = await conn.fetch(_THEMES_SQL, tenant_id, window_days, limit)
    themes = [
        KnowledgeGapTheme(
            norm_q=r["norm_q"],
            example_question=r["example_question"],
            ask_count=int(r["ask_count"]),
            recent_count=int(r["recent_count"]),
            last_seen=r["last_seen"].isoformat(),
            handoff_count=int(r["handoff_count"]),
            no_retrieval_count=int(r["no_retrieval_count"]),
            low_retrieval_count=int(r["low_retrieval_count"]),
        )
        for r in rows
    ]
    return int(total), themes


async def gap_examples(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    norm_q: str,
    window_days: int,
    limit: int,
) -> list[KnowledgeGapExample]:
    """Return raw miss rows for one normalized theme key, newest-first.

    ``norm_q`` must be the exact value returned by ``summarize_gaps`` (the same
    normalization expression is applied server-side so matches are consistent).
    The explicit ``tenant_id = $1`` filter is defence-in-depth alongside RLS.
    """
    rows = await conn.fetch(_EXAMPLES_SQL, tenant_id, window_days, norm_q, limit)
    return [
        KnowledgeGapExample(
            conversation_id=r["conversation_id"],
            created_at=r["created_at"].isoformat(),
            customer_question=r["customer_question"],
            signal_type=r["signal_type"],
        )
        for r in rows
    ]
