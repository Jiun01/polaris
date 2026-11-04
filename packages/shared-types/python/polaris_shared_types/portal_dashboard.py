"""Portal Dashboard aggregate shape — Pydantic SSOT (Phase 8 PR-1).

Drives the Dashboard screen (#1) per 00a §6.13 #1 + portal_design_prompt.md §1.
Aggregates ~6 reads (quota, recent conversations, broadcast count, promotion
count, ingestion-in-flight, 7-day sparkline, suspension state) into one
envelope so the Dashboard loads in one round-trip rather than waterfall.

Backing endpoint: ``GET /api/tenant/dashboard/summary`` — implementation
lands in Phase 8 PR-3 (shell + dashboard); shape ships in PR-1 so the
backend service module can import it.

ConfigDict(extra='forbid') so a future-added widget field that doesn't
make it into the shape can't silently render-as-empty.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from polaris_shared_types.conversation_view import ConversationChannel

_FORBID = ConfigDict(extra="forbid")


class DashboardQuotaSummary(BaseModel):
    """Quota dials block.

    Sourced from tenant_quotas (locked 16 columns per 05 §4.2) + period
    counters maintained by Phase 4 (bot_replies), Phase 3 (knowledge),
    Phase 6 (broadcasts), Phase 7 (bookings).
    """

    model_config = _FORBID

    bot_replies_used:        int   = Field(..., ge=0)
    bot_replies_cap:         int   = Field(..., ge=0)
    knowledge_chunks_used_mb: float = Field(..., ge=0)
    knowledge_chunks_cap_mb:  float = Field(..., ge=0)
    broadcasts_sent_this_period: int = Field(..., ge=0)
    bookings_made_this_period:   int = Field(..., ge=0)


class DashboardRecentConversation(BaseModel):
    """One row in the 5-row recent-conversations preview.

    Sourced from conversations + last messages.intent_classified value.
    Read-only viewer; replies live in Chatwoot (per portal_design_prompt.md §2).
    """

    model_config = _FORBID

    conversation_id:  UUID
    channel:          ConversationChannel
    customer_name:    str | None
    last_message_at:  str = Field(..., description="RFC3339 UTC.")
    message_count:    int = Field(..., ge=0)
    intent:           str | None = Field(
        None,
        description="Last classified intent tag (e.g. 'booking', 'support', 'lead').",
    )
    status:           Literal["open", "resolved", "pending", "snoozed"]


class DashboardSummary(BaseModel):
    """Full envelope for the Dashboard screen.

    Single-round-trip per 04i §6.1: avoids a 6-call waterfall in the
    initial paint. p95 budget ≤ 400 ms server-side.
    """

    model_config = _FORBID

    plan_tier: Literal["starter", "pro", "ultra", "enterprise"] = Field(
        "starter",
        description=(
            "Tenant's current plan tier (mirrors tenants.plan_tier). "
            "Drives the plan label on the Dashboard/Billing screens; "
            "without it the portal falls back to 'starter' and a Pro "
            "tenant is mislabelled."
        ),
    )
    quota: DashboardQuotaSummary
    recent_conversations: list[DashboardRecentConversation] = Field(
        ...,
        max_length=5,
        description="Last 5 most-recently-active conversations.",
    )
    active_broadcasts_count:  int = Field(
        ...,
        ge=0,
        description="Count of broadcasts in scheduled|sending status.",
    )
    active_promotions_count:  int = Field(
        ...,
        ge=0,
        description="Count of tenant_promotions where is_active=true and now() within valid_from..valid_until.",
    )
    ingestion_jobs_in_flight: int = Field(
        ...,
        ge=0,
        description="Count of ingestion_requests in pending|running status.",
    )
    quota_replies_7d_sparkline: list[int] = Field(
        ...,
        min_length=7,
        max_length=7,
        description=(
            "Last 7 days' daily bot_reply counts, oldest-first. "
            "Fixed length 7 so the portal sparkline component doesn't "
            "have to defend against ragged arrays."
        ),
    )
    suspension_state: Literal["quota_exhausted", "payment_overdue"] | None = Field(
        None,
        description=(
            "Mirrors tenants.suspended_reason. NULL = active. "
            "quota_exhausted → red banner, bot off, Chatwoot active (ADR-018). "
            "payment_overdue → route-takeover to /pay (ADR-049) — Dashboard "
            "rarely renders in this state since middleware redirects, but the "
            "field is here for completeness."
        ),
    )


__all__ = [
    "DashboardQuotaSummary",
    "DashboardRecentConversation",
    "DashboardSummary",
]
