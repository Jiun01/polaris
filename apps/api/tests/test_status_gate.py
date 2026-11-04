"""Phase 2 PR-3 — pure-decision tests for ``webhooks._status_gate.decide``.

The branch table is locked by ADR-018 + ADR-049 Amendment 3rd. These
tests parametrise over every cell so a future edit to the decision
function fails loudly if the mapping drifts. DB-free; runs in CI
without ``POLARIS_DATABASE_URL``.
"""

from __future__ import annotations

import pytest
from polaris_api.webhooks._status_gate import (
    DECISION_TO_EVENT_STATUS,
    StatusDecision,
    decide,
)


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        # Happy path: tenant active → proceed.
        ("active", None, StatusDecision.PROCEED),
        ("active", "quota_exhausted", StatusDecision.PROCEED),  # reason ignored when active
        # ADR-018: quota_exhausted suspension → Chatwoot human path.
        ("suspended", "quota_exhausted", StatusDecision.ROUTE_CHATWOOT_HUMAN),
        # ADR-049 Am 3rd: payment_overdue → silent drop.
        ("suspended", "payment_overdue", StatusDecision.DROP_SILENTLY),
        # Suspended with unknown / NULL reason → fail closed to DROP_DELETED.
        ("suspended", None, StatusDecision.DROP_DELETED),
        ("suspended", "mystery_reason", StatusDecision.DROP_DELETED),
        # Deleted (soft-delete window per ADR-029) → drop.
        ("deleted", None, StatusDecision.DROP_DELETED),
        # Unknown / NULL status (defensive — concurrent hard-delete race) → drop.
        (None, None, StatusDecision.DROP_DELETED),
        ("future_unknown_status", None, StatusDecision.DROP_DELETED),
    ],
)
def test_decide_branch_table(
    status: str | None,
    reason: str | None,
    expected: StatusDecision,
) -> None:
    assert decide(status, reason) is expected


def test_event_status_mapping_is_total() -> None:
    """Every ``StatusDecision`` member must have a corresponding
    ``webhook_events.status`` text. A future enum addition without the
    mapping update fails this test (catches the drift in CI)."""
    for decision in StatusDecision:
        assert decision in DECISION_TO_EVENT_STATUS
        # Mapped value must be in the CHECK constraint set on the table.
        assert DECISION_TO_EVENT_STATUS[decision] in {
            "received",
            "suspended_quota",
            "suspended_payment",
            "suspended_deleted",
        }


def test_status_decision_is_str_subclass() -> None:
    """Inheriting from ``str`` lets log lines + JSON dumps consume the
    member without a ``.value`` deref. Regression guard."""
    assert isinstance(StatusDecision.PROCEED, str)
    assert StatusDecision.PROCEED == "proceed"
