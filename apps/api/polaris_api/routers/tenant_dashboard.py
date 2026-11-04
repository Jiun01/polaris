"""Tenant Dashboard aggregate endpoint — Phase 8 PR-3.

Mounted at ``/api/tenant/dashboard``.

One endpoint:

  * ``GET /summary`` — aggregates quota dials, recent conversations,
                       active broadcasts count, active promotions
                       count, ingestion jobs in flight, the 7-day
                       reply-count sparkline, and the suspension state
                       into one envelope. Single round-trip per the
                       p95 ≤ 400 ms budget called out in 04i §6.1.

Why aggregate server-side: the Dashboard screen otherwise needs ~6
parallel HTTP calls from the portal, which (a) costs head-of-line
latency under HTTP/1.1 connection-per-origin limits and (b) gives the
browser a flash of partially-loaded widgets while waterfalls resolve.
The aggregate query reads cheap COUNTs from already-indexed tables, so
the server-side cost stays under the per-request budget.

Auth: standard ``get_tenant_context`` portal-JWT flow. RLS scopes
every read to the calling tenant.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends
from polaris_shared_types.conversation_view import ConversationChannel
from polaris_shared_types.portal_dashboard import (
    DashboardQuotaSummary,
    DashboardRecentConversation,
    DashboardSummary,
)

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.knowledge_ingestion import get_effective_cap_mb

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-dashboard"])


# =====================================================================
# Queries
# =====================================================================

# Pulls every quota counter we expose on the Dashboard in one shot.
# COALESCE wraps each column so a Starter tenant whose tenant_quotas
# row is missing (shouldn't happen post-provisioning but defensively
# possible) doesn't NPE the UI.
_QUOTA_SQL = """
SELECT
    COALESCE(tq.bot_replies_used_this_period,         0)::int AS bot_replies_used,
    COALESCE(tq.bot_replies_period_cap,               0)::int AS bot_replies_cap,
    COALESCE(tq.knowledge_chunks_total_mb_used,     0.0)::float8 AS knowledge_used_mb,
    COALESCE(tq.knowledge_chunks_total_mb_cap,      0.0)::float8 AS knowledge_cap_mb,
    COALESCE(tq.broadcasts_sent_this_period,          0)::int AS broadcasts_sent,
    COALESCE(tq.bookings_made_this_period,            0)::int AS bookings_made,
    t.plan_tier::text AS plan_tier,
    t.suspended_reason
  FROM tenants t
  LEFT JOIN tenant_quotas tq ON tq.tenant_id = t.id
 WHERE t.id = $1
"""

# Every SELECT carries an explicit `WHERE tenant_id = $1` defence-in-depth
# filter alongside the RLS GUC. If RLS is ever misconfigured (e.g. a policy
# DROP slips into a future migration, or a service-pool connection is reused
# accidentally), the explicit clause prevents cross-tenant data leakage.
# RLS is the primary control; the WHERE clause is belt-and-suspenders.

_RECENT_CONVERSATIONS_SQL = """
SELECT c.id              AS conversation_id,
       c.channel,
       COALESCE(
         NULLIF(c.metadata->>'display_name', ''),
         'Customer …' || RIGHT(c.channel_user_id, 4)
       )                  AS customer_name,
       c.last_message_at,
       COALESCE(mc.n, 0)  AS message_count,
       li.intent          AS intent,
       'open'::text       AS status
  FROM conversations c
  LEFT JOIN LATERAL (
        SELECT COUNT(*)::int AS n
          FROM messages m
         WHERE m.conversation_id = c.id
  ) mc ON TRUE
  LEFT JOIN LATERAL (
        SELECT m.intent
          FROM messages m
         WHERE m.conversation_id = c.id
           AND m.intent IS NOT NULL
         ORDER BY m.created_at DESC
         LIMIT 1
  ) li ON TRUE
 WHERE c.tenant_id = $1
   AND c.last_message_at IS NOT NULL
 ORDER BY c.last_message_at DESC
 LIMIT 5
"""

_ACTIVE_BROADCASTS_SQL = """
SELECT COUNT(*)::int AS n
  FROM broadcasts
 WHERE tenant_id = $1
   AND status IN ('scheduled', 'running')
"""

_ACTIVE_PROMOTIONS_SQL = """
SELECT COUNT(*)::int AS n
  FROM tenant_promotions
 WHERE tenant_id = $1
   AND is_active
   AND (valid_from IS NULL OR valid_from <= now())
   AND (valid_until IS NULL OR valid_until >= now())
"""

_INGESTION_IN_FLIGHT_SQL = """
SELECT COUNT(*)::int AS n
  FROM ingestion_requests
 WHERE tenant_id = $1
   AND status IN ('pending', 'processing')
"""

# 7-day daily reply count, bucketed by UTC day. We anchor the cutoff
# to midnight UTC of (today - 6 days) so the window is always exactly
# 7 calendar days — `now() - 6d` would produce a partial leftmost day
# whose count depends on what time the request fired, biasing the
# sparkline visually.
_SPARKLINE_SQL = """
SELECT date_trunc('day', m.created_at)::date AS day,
       COUNT(*)::int                          AS replies
  FROM messages m
  JOIN conversations c ON c.id = m.conversation_id
 WHERE c.tenant_id = $1
   AND m.role = 'assistant'
   AND m.created_at >= $2
 GROUP BY date_trunc('day', m.created_at)
"""


def _midnight_utc_minus_n_days(n: int) -> datetime:
    """Midnight UTC of (today - n days).

    Anchoring to midnight (not now() - Nd) makes the sparkline window
    exactly N calendar days regardless of when the request fires. A
    partial leftmost day would otherwise bias the visual.
    """
    today_utc = datetime.now(UTC).date()
    return datetime.combine(today_utc - timedelta(days=n), datetime.min.time(), tzinfo=UTC)


def _fill_sparkline(
    rows: list[asyncpg.Record], days: int
) -> list[int]:
    """Return a fixed-length list[int] of length `days`, oldest first.

    The SQL returns one row per day-with-replies; days with no replies
    are absent. We fill zeros so the portal's sparkline component
    doesn't have to defend against ragged arrays.
    """
    today_utc = datetime.now(UTC).date()
    by_day = {cast(date, r["day"]): cast(int, r["replies"]) for r in rows}
    out: list[int] = []
    for i in range(days - 1, -1, -1):
        d = today_utc - timedelta(days=i)
        out.append(by_day.get(d, 0))
    return out


def _row_to_recent(r: asyncpg.Record) -> DashboardRecentConversation:
    last = cast(datetime, r["last_message_at"])
    return DashboardRecentConversation(
        conversation_id=cast(UUID, r["conversation_id"]),
        channel=cast(ConversationChannel, r["channel"]),
        customer_name=r["customer_name"],
        last_message_at=last.isoformat(),
        message_count=int(r["message_count"]) if r["message_count"] is not None else 0,
        intent=r["intent"],
        status=cast(
            Literal["open", "resolved", "pending", "snoozed"], r["status"]
        ),
    )


# =====================================================================
# Handler
# =====================================================================


@router.get(
    "/summary",
    response_model=DashboardSummary,
    summary="Aggregate Dashboard data in one round-trip",
    responses={
        401: {"description": "missing_bearer_token or invalid_token"},
        403: {"description": "service_suspended (payment_overdue tenants only)"},
        404: {"description": "tenant_not_found"},
    },
)
async def dashboard_summary(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> DashboardSummary:
    """Single endpoint backing the Dashboard screen.

    Five sequential reads on one asyncpg connection. Sequential on one
    connection avoids connection-acquisition overhead but does NOT
    eliminate per-query RTT — five COUNTs at ~5 ms RTT each add ~25 ms
    over the wall-clock baseline. We accept that within the 400 ms
    p95 budget rather than fan out across connections, because the
    single-connection path keeps the RLS GUC + transaction boundary
    intact for free; a multi-connection variant would have to set
    the GUC on each pooled acquisition.

    If the budget ever tightens, these five reads can collapse into
    one SQL CTE.
    """
    quota_row = await conn.fetchrow(_QUOTA_SQL, ctx.tenant_id)
    if quota_row is None:
        # Tenant row missing — shouldn't happen post-provisioning but
        # surface a clear shape instead of crashing the page render.
        quota = DashboardQuotaSummary(
            bot_replies_used=0,
            bot_replies_cap=0,
            knowledge_chunks_used_mb=0.0,
            knowledge_chunks_cap_mb=0.0,
            broadcasts_sent_this_period=0,
            bookings_made_this_period=0,
        )
        suspension_state: Literal["quota_exhausted", "payment_overdue"] | None = None
        plan_tier: Literal["starter", "pro", "ultra", "enterprise"] = "starter"
    else:
        # Knowledge cap: read the SAME effective cap the /knowledge/capacity
        # endpoint uses (tier default + tenant_billing override) instead of the
        # raw `knowledge_chunks_total_mb_cap` column, which is unpopulated (0)
        # for these tenants — that mismatch made the Billing tile show 0 MB
        # while the Knowledge page showed the real cap (QA finding M3).
        knowledge_cap_mb = await get_effective_cap_mb(conn, ctx.tenant_id)
        quota = DashboardQuotaSummary(
            bot_replies_used=int(quota_row["bot_replies_used"]),
            bot_replies_cap=int(quota_row["bot_replies_cap"]),
            knowledge_chunks_used_mb=float(quota_row["knowledge_used_mb"]),
            knowledge_chunks_cap_mb=float(knowledge_cap_mb),
            broadcasts_sent_this_period=int(quota_row["broadcasts_sent"]),
            bookings_made_this_period=int(quota_row["bookings_made"]),
        )
        suspension_state = cast(
            Literal["quota_exhausted", "payment_overdue"] | None,
            quota_row["suspended_reason"],
        )
        plan_tier = cast(
            Literal["starter", "pro", "ultra", "enterprise"],
            quota_row["plan_tier"],
        )

    recent_rows = await conn.fetch(_RECENT_CONVERSATIONS_SQL, ctx.tenant_id)
    recent = [_row_to_recent(r) for r in recent_rows]

    bcast_row = await conn.fetchrow(_ACTIVE_BROADCASTS_SQL, ctx.tenant_id)
    promo_row = await conn.fetchrow(_ACTIVE_PROMOTIONS_SQL, ctx.tenant_id)
    ingest_row = await conn.fetchrow(_INGESTION_IN_FLIGHT_SQL, ctx.tenant_id)
    spark_rows = await conn.fetch(
        _SPARKLINE_SQL, ctx.tenant_id, _midnight_utc_minus_n_days(6)
    )

    return DashboardSummary(
        plan_tier=plan_tier,
        quota=quota,
        recent_conversations=recent,
        active_broadcasts_count=int(bcast_row["n"]) if bcast_row else 0,
        active_promotions_count=int(promo_row["n"]) if promo_row else 0,
        ingestion_jobs_in_flight=int(ingest_row["n"]) if ingest_row else 0,
        quota_replies_7d_sparkline=_fill_sparkline(list(spark_rows), days=7),
        suspension_state=suspension_state,
    )
