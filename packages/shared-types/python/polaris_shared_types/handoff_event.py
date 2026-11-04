"""``handoff.initiated`` outbound event row — Pydantic SSOT (Phase 7 PR-1).

Persisted to ``webhook_events_outbound`` (migration 047, Phase 6) per
Q-PHASE7-08: Phase 7 writes the rows but does NOT dispatch them. The
v3.0 outbound webhook delivery worker will read this row format when it
ships.

Aligned with the catalog entry in
``docs/Polaris V2/00b_PRD_API_DATA.md §7`` (industry past-tense convention
per ADR-051). The original spec lists the row as ``handoff.initiated``;
this file fixes the on-disk payload shape so v3.0 worker can deserialise
without guessing.

ConfigDict(extra='forbid') so a future row carrying a field unknown to
the v3.0 worker fails its parser, prompting a forward-compat migration
rather than silent data loss.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from polaris_shared_types.conversation_view import ConversationChannel

_FORBID = ConfigDict(extra="forbid")


class HandoffInitiatedEventPayload(BaseModel):
    """Payload column of a webhook_events_outbound row when event_type='handoff.initiated'.

    All UUIDs are tenant-scoped (audit trail). Timestamps are RFC3339 UTC
    strings, not native datetimes, because asyncpg's JSONB serialiser
    rejects datetime objects — the convention across all
    webhook_events_outbound payloads is string-timestamps.
    """

    model_config = _FORBID

    version: Literal[1] = Field(
        1,
        description=(
            "Payload version. Bumps when the schema breaks. v3.0 worker "
            "switches on this field to select the deserialiser."
        ),
    )
    tenant_id: UUID
    conversation_id: UUID = Field(
        ...,
        description="Polaris conversations.id — joins to messages.conversation_id.",
    )
    channel: ConversationChannel
    requested_at: str = Field(
        ...,
        description=(
            "RFC3339 UTC. When the customer's handoff intent was classified — "
            "i.e. the timestamp of the inbound message that triggered handoff."
        ),
    )
    initiated_at: str = Field(
        ...,
        description="RFC3339 UTC. When Polaris's handoff service started the orchestration.",
    )
    reason: Literal["intent_classified", "stop_keyword", "flow_node"] = Field(
        "intent_classified",
        description=(
            "Why the handoff fired. ``intent_classified`` = LLM detected "
            "handoff intent. ``stop_keyword`` = STOP / BERHENTI matcher fired "
            "(future v2.1 — currently STOP triggers opt-out, not handoff). "
            "``flow_node`` = tenant-authored flow explicitly routes via a "
            "handoff node."
        ),
    )
    chatwoot_conversation_id: int | None = Field(
        None,
        description=(
            "Chatwoot's conversation.id once the status flip succeeded. "
            "NULL on outcome != CONFIRMED (Chatwoot never accepted the flip)."
        ),
    )
    team_id: str = Field(
        ...,
        description=(
            "HandoffNode.team_id from the flow definition (Q-PHASE7-05: "
            "per-flow source only). String not int because flow definitions "
            "carry team_ids as strings (Chatwoot team_id is int — translated "
            "at the client layer)."
        ),
    )
    business_hours_state: Literal["in_hours", "after_hours"] = Field(
        ...,
        description=(
            "Which handoff_message variant was sent to the customer "
            "(handoff_message_in_hours vs handoff_message_after_hours). "
            "ADR-019."
        ),
    )


__all__ = [
    "HandoffInitiatedEventPayload",
]
