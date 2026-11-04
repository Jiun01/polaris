"""Broadcast + audience-filter Pydantic SSOT (Phase 6 PR-1).

Three concepts live in this module:

  1. **AudienceFilter** — discriminated union on ``filter_kind`` over 7
     variants (all_opted_in, tags_any, tags_all, branch, locale, channel,
     combined_and). Compiled at materialisation time by
     ``services/audience/filter.compile_audience_filter`` into a
     parameterised SQL WHERE clause — NEVER string-concatenated, NEVER
     interpolated through ``f"..."``. The shape is closed (Pydantic
     ``extra='forbid'``) so a Phase 8 portal canvas evolution that adds
     a new filter kind fails fast at the API boundary instead of producing
     an over-broad audience (compliance hazard).

  2. **Broadcast lifecycle DTOs** — ``CreateBroadcastRequest`` for the
     POST surface, ``BroadcastDTO`` for the GET surface. ``BroadcastStatus``
     + ``BroadcastRecipientStatus`` enum literals are aligned with the
     PostgreSQL ENUM types in migrations 042 / 043.

  3. **Tier-gate marker** — broadcasts are Pro+ only per ADR-026
     Amendment 3rd. This is enforced at the *service* layer (HTTP 403
     ``feature_not_in_tier``) — there's no enum or flag in this module
     that captures it. Reviewers looking for "where is the tier check?"
     should look in services/broadcast/service.create_broadcast.

``combined_and`` is depth=1 only at GA: ``parts`` cannot itself contain a
``combined_and``. The ``model_validator`` on ``FilterCombinedAnd`` enforces
this. Depth >= 2 requires a CTE rewrite of the SQL compiler and is
deferred per 04g §11 (Q-PHASE6 doesn't currently carry a number for this
— track via the deep-dive's `combined_and` section if asked).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FORBID = ConfigDict(extra="forbid")

# ---------------------------------------------------------------------
# Lifecycle enums (aligned with PostgreSQL ENUM types in migrations
# 042 / 043; drift surfaces in the round-trip tests under
# apps/api/tests/outbound/test_broadcast_service.py)
# ---------------------------------------------------------------------

BroadcastStatus = Literal[
    "draft", "scheduled", "running", "paused", "completed", "cancelled", "errored"
]

BroadcastRecipientStatus = Literal[
    "pending", "skipped", "sent", "delivered", "read", "failed"
]

# ---------------------------------------------------------------------
# Audience filter discriminated union
# ---------------------------------------------------------------------


class FilterAllOptedIn(BaseModel):
    """Match every audience_members row where opted_out_at IS NULL.

    The compiler always adds ``opted_out_at IS NULL`` to the outer
    WHERE regardless of filter; this kind exists for tenants who want
    "the whole list" without specifying any sub-criteria.
    """

    model_config = _FORBID
    filter_kind: Literal["all_opted_in"]


class FilterTagsAny(BaseModel):
    """Match if audience_members.tags overlaps the given set (&&)."""

    model_config = _FORBID
    filter_kind: Literal["tags_any"]
    tags: list[str] = Field(min_length=1, max_length=20)


class FilterTagsAll(BaseModel):
    """Match if audience_members.tags is a superset of the given set (@>)."""

    model_config = _FORBID
    filter_kind: Literal["tags_all"]
    tags: list[str] = Field(min_length=1, max_length=20)


class FilterBranch(BaseModel):
    """Match a specific tenant_branch_id (Pro+ multi-branch tenants)."""

    model_config = _FORBID
    filter_kind: Literal["branch"]
    tenant_branch_id: UUID


class FilterLocale(BaseModel):
    """Match audience_members.locale ∈ given set."""

    model_config = _FORBID
    filter_kind: Literal["locale"]
    locales: list[str] = Field(min_length=1, max_length=10)


class FilterChannel(BaseModel):
    """Match audience_members.channel ∈ given set.

    Telegram is in the type union for v2.1 forward-compat but rejected
    at the validation boundary today via the Literal restriction.
    """

    model_config = _FORBID
    filter_kind: Literal["channel"]
    channels: list[Literal["whatsapp", "messenger"]] = Field(
        min_length=1, max_length=2
    )


class FilterCombinedAnd(BaseModel):
    """AND-combine 2..4 sub-filters; depth=1 only at GA.

    Nesting a combined_and inside another combined_and is rejected by
    the ``model_validator`` below. The SQL compiler would need a CTE
    rewrite to support depth>=2 cleanly (parenthesised AND groups
    nested under another AND group); deferred.
    """

    model_config = _FORBID
    filter_kind: Literal["combined_and"]
    parts: list[
        FilterAllOptedIn | FilterTagsAny | FilterTagsAll | FilterBranch | FilterLocale | FilterChannel
    ] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def _no_nested_combined(self) -> FilterCombinedAnd:
        # Defence-in-depth: the type union above already excludes
        # FilterCombinedAnd from parts[], so this check is currently
        # unreachable. Kept as a guard so future contributors who try
        # to widen the union for v2.1+ have a clear failure path
        # pointing back at this depth-limit decision.
        for part in self.parts:
            if isinstance(part, FilterCombinedAnd):  # pragma: no cover
                raise ValueError(
                    "combined_and depth=1 only at GA; deferred to v2.1+"
                )
        return self


AudienceFilter = Annotated[
    FilterAllOptedIn | FilterTagsAny | FilterTagsAll | FilterBranch | FilterLocale | FilterChannel | FilterCombinedAnd,
    Field(discriminator="filter_kind"),
]
"""Discriminated union over the 7 GA filter kinds.

Pydantic resolves the right variant by reading the ``filter_kind`` field
on incoming JSON. An unknown ``filter_kind`` raises 422 at the API
boundary with a clear error rather than silently matching the wrong
shape.
"""

# ---------------------------------------------------------------------
# Broadcast lifecycle DTOs
# ---------------------------------------------------------------------


class CreateBroadcastRequest(BaseModel):
    """POST /api/tenant/broadcasts body.

    ``scheduled_at=None`` => send immediately (the service layer sets
    ``scheduled_at = now()`` at materialisation time). Future scheduling
    requires a UTC timestamp; the worker drains based on
    ``ZRANGEBYSCORE broadcast:queue:{tenant_id} -inf <now>``.
    """

    model_config = _FORBID

    name: str = Field(min_length=1, max_length=200)
    tenant_channel_id: UUID
    template_id: UUID
    audience_filter: AudienceFilter
    variables_template: dict[str, str] = Field(default_factory=dict)
    scheduled_at: datetime | None = None


class BroadcastDTO(BaseModel):
    """GET /api/tenant/broadcasts/{id} response shape.

    Counters are read directly off the broadcasts row — they're
    denormalised projections of broadcast_recipients.status counts,
    maintained incrementally by the worker + the wa-status webhook.
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    tenant_channel_id: UUID
    template_id: UUID
    name: str
    status: BroadcastStatus
    audience_filter: AudienceFilter
    variables_template: dict[str, str]
    scheduled_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    total_recipients: int
    sent_count: int
    delivered_count: int
    read_count: int
    failed_count: int
    skipped_count: int
    error: dict[str, object] | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class BroadcastRecipientDTO(BaseModel):
    """GET /api/tenant/broadcasts/{id}/recipients row.

    ``skip_reason`` is a free-text column in the DB (see migration 043
    header for the canonical values); we don't enum-restrict it here so
    that a forward-compat operational marker (e.g. an admin manually
    setting a recipient aside with a custom reason) doesn't fail
    validation on the read path.
    """

    model_config = _FORBID

    id: UUID
    broadcast_id: UUID
    audience_member_id: UUID
    channel: str
    channel_user_id: str
    status: BroadcastRecipientStatus
    wa_message_id: str | None
    variables_resolved: dict[str, str]
    skip_reason: str | None
    attempt_count: int
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    failed_at: datetime | None
    failure_payload: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "AudienceFilter",
    "BroadcastDTO",
    "BroadcastRecipientDTO",
    "BroadcastRecipientStatus",
    "BroadcastStatus",
    "CreateBroadcastRequest",
    "FilterAllOptedIn",
    "FilterBranch",
    "FilterChannel",
    "FilterCombinedAnd",
    "FilterLocale",
    "FilterTagsAll",
    "FilterTagsAny",
]
