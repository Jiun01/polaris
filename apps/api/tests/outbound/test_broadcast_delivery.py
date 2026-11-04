"""Broadcast delivery state-machine tests — Phase 6 PR-7.

Pure-logic coverage of the state ranks + Meta vocab mapping. The
end-to-end wamid -> recipient row resolution against real DB lives
in CI's migration-check workflow.
"""

from __future__ import annotations

from polaris_api.services.broadcast import delivery


def test_state_ranks_are_monotonic_forward() -> None:
    """sent < delivered < read. failed is highest so it can override
    any forward state (post-delivery customer-side failure)."""
    assert delivery._RANK["sent"] < delivery._RANK["delivered"]  # noqa: SLF001
    assert delivery._RANK["delivered"] < delivery._RANK["read"]  # noqa: SLF001
    assert delivery._RANK["failed"] > delivery._RANK["read"]  # noqa: SLF001


def test_pending_lowest_rank() -> None:
    """pending is the entry point — anything else is "further along"."""
    rank = delivery._RANK  # noqa: SLF001
    for status in ("sent", "delivered", "read", "failed"):
        assert rank["pending"] < rank[status]


def test_outcome_constants_distinct() -> None:
    """The 4 outcome strings must be distinct so test assertions
    + log filtering work cleanly."""
    outcomes = {
        delivery.DeliveryOutcome.APPLIED,
        delivery.DeliveryOutcome.NOOP_UNKNOWN_WAMID,
        delivery.DeliveryOutcome.NOOP_REGRESSION,
        delivery.DeliveryOutcome.NOOP_IDEMPOTENT,
    }
    assert len(outcomes) == 4
