"""Tenant Knowledge Gaps endpoint — Knowledge Gap Detection / M3.

Mounted at ``/api/tenant/knowledge-gaps``. ULTRA-only.

  * ``GET /summary`` — ranked gap themes (M2-lite normalized-text grouping) for
    the calling tenant over a configurable look-back window, in one round-trip.
  * ``GET /summary/examples`` — raw miss rows for a single normalized theme key,
    for drill-down to contributing conversations.

Gate: ``ctx.plan_tier`` must be in ``settings.knowledge_gap_tiers`` (the same
Ultra set the capture path uses, so feature visibility and data capture can
never drift); otherwise 403 ``KNOWLEDGE_GAPS_TIER_BLOCKED``. RLS scopes every
read to the calling tenant.
"""

from __future__ import annotations

import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from polaris_shared_types.knowledge_gaps import (
    KnowledgeGapExamplesResponse,
    KnowledgeGapsSummary,
)

from polaris_api.config import get_settings
from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.knowledge_gaps import aggregate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-knowledge-gaps"])

# Cap for theme list — client-side sort/filter operates on this bounded set.
_MAX_THEMES = 50

# Cap for per-theme example rows returned by the drill-down endpoint.
_MAX_EXAMPLES = 20

# Allowed look-back windows (days).  Reject anything outside this set with 422
# so unbounded window scans can't reach the DB.
_ALLOWED_WINDOWS: frozenset[int] = frozenset({7, 30, 90})


def _validate_window(window_days: int) -> int:
    """Raise 422 with INVALID_WINDOW if window_days is not in the allow-set."""
    if window_days not in _ALLOWED_WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_WINDOW",
                "hint": f"window_days must be one of {sorted(_ALLOWED_WINDOWS)}.",
            },
        )
    return window_days


@router.get(
    "/summary",
    response_model=KnowledgeGapsSummary,
    summary="Ranked knowledge-gap themes for the tenant (Ultra only)",
    responses={
        401: {"description": "missing_bearer_token or invalid_token"},
        403: {"description": "KNOWLEDGE_GAPS_TIER_BLOCKED — not an Ultra tenant"},
        422: {"description": "INVALID_WINDOW — window_days must be 7, 30, or 90"},
    },
)
async def knowledge_gaps_summary(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    window_days: Annotated[int, Query(description="Look-back window in days. Must be 7, 30, or 90.")] = 30,
) -> KnowledgeGapsSummary:
    """Aggregate the tenant's recent miss rows into ranked themes."""
    if ctx.plan_tier not in get_settings().knowledge_gap_tiers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "KNOWLEDGE_GAPS_TIER_BLOCKED",
                "hint": "Knowledge Gaps is an Ultra feature.",
            },
        )
    _validate_window(window_days)
    total, themes = await aggregate.summarize_gaps(
        conn,
        tenant_id=ctx.tenant_id,
        window_days=window_days,
        limit=_MAX_THEMES,
    )
    return KnowledgeGapsSummary(
        plan_tier=ctx.plan_tier,
        window_days=window_days,
        total_misses=total,
        themes=themes,
    )


@router.get(
    "/summary/examples",
    response_model=KnowledgeGapExamplesResponse,
    summary="Raw miss rows for a single normalized gap theme (Ultra only)",
    responses={
        401: {"description": "missing_bearer_token or invalid_token"},
        403: {"description": "KNOWLEDGE_GAPS_TIER_BLOCKED — not an Ultra tenant"},
        422: {"description": "INVALID_WINDOW — window_days must be 7, 30, or 90"},
    },
)
async def knowledge_gaps_examples(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    q: Annotated[
        str,
        Query(
            min_length=1,
            description="Normalized theme key returned by /summary (norm_q field).",
        ),
    ],
    window_days: Annotated[int, Query(description="Look-back window in days. Must be 7, 30, or 90.")] = 30,
) -> KnowledgeGapExamplesResponse:
    """Return raw knowledge_gap_misses rows for one normalized theme key.

    ``q`` must be the ``norm_q`` value returned by ``GET /summary`` — the same
    normalized string the grouping SQL produces.  Newest-first, capped at 20.
    """
    if ctx.plan_tier not in get_settings().knowledge_gap_tiers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "KNOWLEDGE_GAPS_TIER_BLOCKED",
                "hint": "Knowledge Gaps is an Ultra feature.",
            },
        )
    _validate_window(window_days)
    examples = await aggregate.gap_examples(
        conn,
        tenant_id=ctx.tenant_id,
        norm_q=q,
        window_days=window_days,
        limit=_MAX_EXAMPLES,
    )
    return KnowledgeGapExamplesResponse(examples=examples)
