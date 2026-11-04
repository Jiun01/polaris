"""Chatwoot REST API surface — Pydantic SSOT (Phase 7 PR-1).

Shapes for the outbound calls Polaris makes into Chatwoot's REST API v1
(v4.13.0 contract). These wrap request envelopes, response payloads,
and the handoff-orchestration result type that services/chatwoot/handoff.py
returns to the flow runtime adapter.

The Chatwoot v4.13.0 envelope shape is also locked in the inbound
webhook handler (apps/api/polaris_api/webhooks/chatwoot_router.py). If
upstream Chatwoot rebases bring schema drift, both surfaces need a
synchronised update — track via CW fork FORK_MAINTENANCE.md §E
(contract surface category).

``ConfigDict(extra='forbid')`` everywhere so Chatwoot envelope drift
surfaces during integration tests instead of being silently dropped.

Vocabulary modelled as ``Literal[...]`` type aliases (not ``Enum``
classes) per project convention — see tenant_template.py for the
established pattern. Aligns with Phase 6's TemplateStatus / OptOutReason
shape so the TS export pipeline produces consistent string-literal
unions across phases.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


ChatwootConversationStatus = Literal["open", "resolved", "pending", "snoozed"]
"""Chatwoot conversation status vocabulary.

Phase 7 only WRITES ``"open"`` (via flip_conversation_status on handoff).
Other values exist in the Chatwoot wire format and are listed here for
type-safety on any future read path — they are NOT currently targets
for outbound writes.
"""


HandoffOutcome = Literal["confirmed", "declined", "errored"]
"""Result of services/chatwoot/handoff.py::initiate_handoff.

Per ADR-051:
  ``"confirmed"`` — customer received handoff message AND Chatwoot
    accepted status flip + team assignment + private note. Agent will
    pick up the conversation in their queue.
  ``"declined"`` — flow false-branch carrying TRANSIENT-infra signal.
    Chatwoot was unreachable and the retry budget was consumed.
    The customer is NOT left silent — the configured handoff message
    still went out via the channel dispatcher. Operator action:
    investigate Chatwoot health.
  ``"errored"`` — flow false-branch carrying STRUCTURAL signal.
    Chatwoot returned 4xx (e.g. team_id doesn't exist, conversation
    already closed). Operator action: check flow configuration.

Both ``declined`` and ``errored`` route to the same handoff-node false
edge in the flow runtime; the distinction is captured in
flow_run.step_trace so operators can tell "Chatwoot was down" from
"the flow author misconfigured".
"""


ChatwootLabelTarget = Literal["conversation", "contact"]
"""Whether a label is applied to a Chatwoot conversation or contact.

``"contact"`` carries the PDPA opt-out label (``pol_opted_out``) since
opt-out is per-person, not per-conversation. ``"conversation"`` carries
the bot-suspension label (``pol_bot_suspended``) since suspension
applies to specific conversations even though it's triggered at the
tenant level.
"""


HandoffPendingRecoveryReason = Literal[
    "chatwoot_5xx_after_retries",
    "chatwoot_4xx",
    "chatwoot_timeout",
]
"""Q-PHASE7-04 partial-failure classifier.

Written to ``conversations.handoff_pending_recovery_reason`` when
handoff customer-message succeeded but the Chatwoot path failed. Drives
the recovery-note template selection on next inbound.

Values match migration 049's COMMENT ON COLUMN copy verbatim — if you
add a new classifier here, update the migration comment too (the
column is a free-text ``TEXT``; semantic vocabulary lives in this
Literal, not in a DB CHECK).
"""


class ChatwootRequestEnvelope(BaseModel):
    """Wraps every outbound Chatwoot call for audit + log uniformity.

    Carried through the client so cross-cutting concerns (logging,
    tracing, idempotency-on-retry) have a single shape to inspect. Not
    serialised into the Chatwoot request body — that's per-verb.
    """

    model_config = _FORBID

    account_id: int = Field(
        ...,
        gt=0,
        description=(
            "Chatwoot account id, resolved from tenants.chatwoot_account_id "
            "for the calling tenant. URL composition: "
            "POST /api/v1/accounts/{account_id}/... — never hardcoded."
        ),
    )
    idempotency_key: str | None = Field(
        None,
        max_length=128,
        description=(
            "Optional client-side idempotency token. Used by ensure_label "
            "to short-circuit redundant create calls within a single request "
            "lifecycle. Chatwoot does NOT honour this header server-side."
        ),
    )


class HandoffResult(BaseModel):
    """Returned by services/chatwoot/handoff.py::initiate_handoff.

    Carried up through the flow runtime adapter into the handoff node
    executor's NodeResult.decision payload, so flow authors can inspect
    the outcome details via flow_run.step_trace.
    """

    model_config = _FORBID

    outcome: HandoffOutcome
    chatwoot_status_flipped: bool
    team_assigned: bool
    private_note_posted: bool
    customer_message_sent: bool
    customer_message_id: str | None = None
    chatwoot_conversation_id: int | None = None
    handoff_at: str = Field(..., description="RFC3339 UTC instant of completion")
    business_hours_state: Literal["in_hours", "after_hours"]
    reason: str | None = Field(
        None,
        description=(
            "Set when outcome != 'confirmed'. Free-form classifier string "
            "(e.g. 'no_conversation', 'chatwoot_5xx_after_retries', "
            "'team_not_found'). Carried into flow_run.step_trace for audit."
        ),
    )


class ChatwootLabelEnsureResult(BaseModel):
    """Returned by ChatwootClient.ensure_label.

    ``created`` is False when the label already existed (idempotent re-call)
    or when ``CHATWOOT_LABEL_AUTO_PROVISION`` is False and we skipped
    the GET/POST round-trip.
    """

    model_config = _FORBID

    label: str
    created: bool
    account_id: int


__all__ = [
    "ChatwootConversationStatus",
    "ChatwootLabelEnsureResult",
    "ChatwootLabelTarget",
    "ChatwootRequestEnvelope",
    "HandoffOutcome",
    "HandoffPendingRecoveryReason",
    "HandoffResult",
]
