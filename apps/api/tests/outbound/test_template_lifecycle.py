"""Template lifecycle dispatcher tests — Phase 6 PR-3.

Pure-logic coverage of ``services/templates/lifecycle.py`` that
doesn't need a live DB:

  * ``_EVENT_TO_STATUS`` mapping is exhaustive for every Meta vocab
    value we expect to receive.
  * ``apply_status_update`` for an unknown Meta event returns
    ``outcome='noop_unknown_event'`` without touching the DB
    (verified by NOT requiring the service pool to be initialised).

Status-flip + audit + auto-pause cascade are integration tests under
the migration-check CI workflow (DB-required); they skip locally
without POLARIS_TEST_DATABASE_URL.
"""

from __future__ import annotations

import pytest
from polaris_api.services.templates import lifecycle


def test_event_mapping_covers_meta_vocabulary() -> None:
    """Every Meta lifecycle event we currently handle is mapped to a
    valid tenant_templates.status value. Drift here is exactly the
    class of bug that lets a webhook silently no-op."""
    expected = {
        "APPROVED": "approved",
        "REJECTED": "rejected",
        "PAUSED": "paused",
        "DISABLED": "disabled",
        "FLAGGED": "paused",
        "PENDING_DELETION": "disabled",
        "PENDING": "pending",
    }
    assert lifecycle._EVENT_TO_STATUS == expected  # noqa: SLF001


def test_degraded_statuses_cover_pause_cascade_triggers() -> None:
    """The auto-pause cascade fires for rejected / paused / disabled.
    APPROVED + PENDING must NOT trigger pausing (otherwise re-approval
    of a paused template would cascade through a no-op cycle)."""
    assert lifecycle._DEGRADED_STATUSES == {  # noqa: SLF001
        "rejected",
        "paused",
        "disabled",
    }


@pytest.mark.asyncio
async def test_apply_status_update_unknown_event_is_noop() -> None:
    """An unknown Meta event short-circuits before any DB access.

    Meta has historically added new event values without warning;
    raising on unknowns would back up their retry queue. The
    LifecycleResult outcome is ``noop_unknown_event``.
    """
    result = await lifecycle.apply_status_update(
        meta_template_id="TPL_test_001",
        event="SOME_FUTURE_EVENT_META_ADDED_LATER",
        reason=None,
    )
    assert result.outcome == "noop_unknown_event"
    assert result.tenant_id is None
    assert result.broadcasts_paused == 0
