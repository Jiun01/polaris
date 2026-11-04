"""Knowledge Gaps dashboard shapes — Pydantic SSOT (Knowledge Gap Detection M3).

Drives the Ultra-only "Knowledge Gaps" portal screen. The backing endpoints:
  * ``GET /api/tenant/knowledge-gaps/summary`` — aggregates ``knowledge_gap_misses``
    rows into ranked themes server-side (M2-lite: normalized-text grouping, no
    embeddings yet) so the screen loads in one round-trip.
  * ``GET /api/tenant/knowledge-gaps/summary/examples`` — raw miss rows for a
    single normalized theme key (drill-down to contributing conversations).

ConfigDict(extra='forbid') so a future field that doesn't make it into the shape
can't silently render-as-empty.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class KnowledgeGapTheme(BaseModel):
    """One clustered gap — a group of near-identical unanswered questions.

    v1 grouping is by normalized question text (lowercase, punctuation-stripped,
    whitespace-collapsed), so distinct phrasings may not merge until the M2
    semantic-clustering upgrade. ``example_question`` is the most-recent raw
    phrasing in the group, shown to the tenant verbatim.

    ``norm_q`` is the stable grouping key — pass it as the ``q`` param to
    ``GET /summary/examples`` to fetch the contributing raw rows.
    """

    model_config = _FORBID

    norm_q: str = Field(
        ...,
        description=(
            "Stable normalized key for this theme group "
            "(lowercase, punctuation→space, whitespace-collapsed). "
            "Pass as `q` to GET /summary/examples for drill-down."
        ),
    )
    example_question: str = Field(
        ..., description="Most-recent raw phrasing in this group (display verbatim)."
    )
    ask_count: int = Field(
        ..., ge=1, description="Total misses in this theme within the window."
    )
    recent_count: int = Field(
        ...,
        ge=0,
        description="Misses in the last 7 days — a trend hint (rising if a large share of ask_count).",
    )
    last_seen: str = Field(..., description="RFC3339 UTC of the most recent miss.")
    handoff_count: int = Field(..., ge=0, description="Misses signalled by handoff.")
    no_retrieval_count: int = Field(
        ..., ge=0, description="Misses signalled by zero RAG retrieval."
    )
    low_retrieval_count: int = Field(
        ..., ge=0, description="Misses signalled by low RAG retrieval score."
    )


class KnowledgeGapsSummary(BaseModel):
    """Full envelope for the Knowledge Gaps screen — one round-trip."""

    model_config = _FORBID

    plan_tier: str = Field(
        ..., description="Tenant's plan tier (mirrors tenants.plan_tier)."
    )
    window_days: int = Field(
        ..., ge=1, description="Look-back window the themes were aggregated over."
    )
    total_misses: int = Field(
        ..., ge=0, description="Total miss rows in the window (pre-grouping)."
    )
    themes: list[KnowledgeGapTheme] = Field(
        ...,
        description="Ranked gap themes, highest ask_count first (capped server-side).",
    )


class KnowledgeGapExample(BaseModel):
    """One raw miss row for a theme's drill-down view.

    Returned by ``GET /api/tenant/knowledge-gaps/summary/examples``.
    ``conversation_id`` links to the portal conversation detail page.
    """

    model_config = _FORBID

    conversation_id: str = Field(
        ..., description="UUID of the conversation this miss belongs to."
    )
    created_at: str = Field(
        ..., description="ISO 8601 UTC timestamp of when the miss was captured."
    )
    customer_question: str = Field(
        ..., description="Raw inbound question text that went unanswered."
    )
    signal_type: str = Field(
        ..., description="Miss signal: 'no_retrieval', 'low_retrieval', or 'handoff'."
    )


class KnowledgeGapExamplesResponse(BaseModel):
    """Envelope for the drill-down examples endpoint."""

    model_config = _FORBID

    examples: list[KnowledgeGapExample] = Field(
        ...,
        description=(
            "Raw miss rows for the requested theme, newest-first, capped at 20."
        ),
    )


__all__ = [
    "KnowledgeGapExample",
    "KnowledgeGapExamplesResponse",
    "KnowledgeGapTheme",
    "KnowledgeGapsSummary",
]
