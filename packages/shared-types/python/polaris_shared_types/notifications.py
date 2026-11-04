"""Portal in-app notification feed shapes — Pydantic SSOT (Phase 8 PR-1).

Drives the bell-icon dropdown + full-page filtered view per 00a §6.13.1
"Notification center". The portal polls `/api/tenant/notifications` every
30 s while the bell is visible (Q-PHASE8-08); mark-read writes go through
`PATCH /api/tenant/notifications/{id}` or `POST /api/tenant/notifications/mark-all-read`.

Backing table: `notifications` (migration 051). `kind` is an app-side
enum, not a Postgres type, so adding a new event surface only requires
an app deploy.

ConfigDict(extra='forbid') so a future row carrying a field unknown to
the portal fails its parser, prompting a forward-compat migration rather
than silent display-truncation.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class NotificationKind(StrEnum):
    """Catalogued event kinds. Match the comment block on
    ``notifications.kind`` in migration 051.

    Strings (not opaque ints) so the bell-icon UI can switch on the
    value directly without an extra lookup. Past-tense aligned with
    ADR-051's outbound-webhook naming convention.
    """

    TEMPLATE_APPROVED       = "template_approved"
    TEMPLATE_REJECTED       = "template_rejected"
    TEMPLATE_PAUSED         = "template_paused"
    TEMPLATE_DISABLED       = "template_disabled"

    BROADCAST_COMPLETED     = "broadcast_completed"
    BROADCAST_FAILED        = "broadcast_failed"

    INVOICE_ISSUED          = "invoice_issued"
    INVOICE_OVERDUE         = "invoice_overdue"
    INVOICE_PAID            = "invoice_paid"

    PLAN_CHANGE_APPROVED    = "plan_change_approved"
    ACCOUNT_DELETION_GRACE  = "account_deletion_grace_warning"

    INGESTION_COMPLETE      = "ingestion_complete"
    INGESTION_FAILED        = "ingestion_failed"
    INGESTION_PARTIAL       = "ingestion_partial"

    QUOTA_REPLIES_80        = "quota_replies_80"
    QUOTA_REPLIES_100       = "quota_replies_100"

    MM_LITE_STATUS_CHANGED  = "mm_lite_status_changed"

    BRANCH_AGENT_ACCEPTED   = "branch_agent_invite_accepted"

    DEMO_SESSION_CONVERTED  = "demo_session_converted"


class Notification(BaseModel):
    """One row in the portal feed. Shape returned by
    ``GET /api/tenant/notifications`` and ``PATCH .../{id}``.

    Timestamps are RFC3339 UTC strings (asyncpg's JSONB serialiser
    rejects datetime objects, and the column reads come back as
    ``datetime`` which we ``.isoformat()`` at the router layer — the
    string contract here is what the portal sees).
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    recipient_user_id: UUID | None = Field(
        None,
        description="NULL = tenant-wide; non-NULL = single auth.users row.",
    )
    kind: NotificationKind
    title: str = Field(..., min_length=1, max_length=200)
    body: str | None = Field(None, max_length=2000)
    href: str | None = Field(
        None,
        max_length=512,
        description="Click-through path to the relevant portal screen.",
    )
    metadata: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Free-form renderer-side payload (template_id, broadcast_id, "
            "invoice_number, etc.). Schema is per-kind; not validated at "
            "the Notification level so adding a kind doesn't require a "
            "shape bump."
        ),
    )
    read_at: str | None = Field(
        None,
        description="RFC3339 UTC. NULL = unread.",
    )
    created_at: str = Field(..., description="RFC3339 UTC.")


class NotificationListResponse(BaseModel):
    """Envelope for ``GET /api/tenant/notifications``.

    Cursor pagination: ``next_cursor`` is opaque (currently the
    created_at of the last item, but the contract makes no promise so
    we can switch to a tie-breaker key without changing the wire shape).
    """

    model_config = _FORBID

    items: list[Notification]
    unread_count: int = Field(
        ...,
        ge=0,
        description=(
            "Total unread across all kinds for this tenant. Drives the "
            "bell-icon badge; computed cheaply via the partial index "
            "on notifications(tenant_id, created_at DESC) WHERE "
            "read_at IS NULL."
        ),
    )
    next_cursor: str | None = Field(
        None,
        description=(
            "Opaque cursor for the next page. NULL on the last page. "
            "Pass back as ?cursor=… on the follow-up request."
        ),
    )


class NotificationMarkReadRequest(BaseModel):
    """Body for ``PATCH /api/tenant/notifications/{id}``.

    Idempotent: setting read=true twice is a no-op. Setting read=false
    on an already-read notification clears the marker (rare; supports
    the "mark unread" UI affordance).
    """

    model_config = _FORBID

    read: bool


class NotificationMarkAllReadResponse(BaseModel):
    """Response from ``POST /api/tenant/notifications/mark-all-read``."""

    model_config = _FORBID

    marked_count: int = Field(
        ...,
        ge=0,
        description="How many notifications transitioned unread → read.",
    )


__all__ = [
    "Notification",
    "NotificationKind",
    "NotificationListResponse",
    "NotificationMarkAllReadResponse",
    "NotificationMarkReadRequest",
]
