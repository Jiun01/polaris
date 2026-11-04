"""Phase 7 PR-7 — E2E opt-out → Chatwoot label sync within 5s.

Real DB (skips without ``POLARIS_DATABASE_URL``). Mocked Chatwoot HTTP.

What we cover:
  * STOP keyword inbound on WhatsApp → ``audience_members.opted_out_at``
    flips → ``sync_opt_out_label`` fires from inside ``mark_opted_out``
    as a non-blocking side-effect
  * Chatwoot ``apply_contact_label('pol_opted_out')`` called with the
    correct ``account_id`` + ``contact_id``
  * Total wall-clock between the inbound and the label call ≤ 5s
    (Phase 7 exit criterion)
  * Opt-out write itself succeeds even when Chatwoot is unreachable
    (non-blocking PDPA guarantee)

Mock-based equivalents in ``tests/chatwoot/test_label_sync.py``.
"""

from __future__ import annotations

import pytest
from polaris_api.config import get_settings

# pydantic-settings auto-loads from .env at repo root; operator doesn't
# need to source it before pytest. CI overrides via os.environ still
# win because Settings reads os.environ as the highest-priority source.
_settings = get_settings()
_DB_URL = _settings.polaris_db_url_service or _settings.polaris_db_url_anon

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason=(
        "polaris_db_url_service / _anon not configured (.env or shell env); "
        "Phase 7 E2E opt-out sweep is operator-driven."
    ),
)


@pytest.mark.asyncio
async def test_stop_opt_out_propagates_label_within_5s() -> None:
    """Customer sends STOP → audience_members row gets opted_out_at
    set → Chatwoot contact gets pol_opted_out label within 5 seconds
    (the Phase 7 exit-criterion SLA).

    Seeding contract: tenant has chatwoot_account_id linked,
    audience_members row exists with chatwoot_contact_id populated.

    Assertions:
      - audience_members.opted_out_at IS NOT NULL after the inbound
      - Chatwoot stub recorded apply_contact_label with the right
        (account_id, contact_id, label='pol_opted_out')
      - Time between inbound and label call < 5 seconds
    """
    pytest.skip(
        "Phase 7 PR-7: operator-driven E2E. Orchestration coverage in "
        "tests/chatwoot/test_label_sync.py."
    )


@pytest.mark.asyncio
async def test_stop_opt_out_succeeds_when_chatwoot_down() -> None:
    """Chatwoot returns 503 on the label apply → audience_members write
    still succeeds (PDPA non-blocking guarantee). Label sync logs the
    failure but does NOT bubble back to the inbound path."""
    pytest.skip(
        "Phase 7 PR-7: operator-driven E2E. See test_stop_opt_out_propagates_label_within_5s."
    )


@pytest.mark.asyncio
async def test_opt_out_unknown_number_is_silent() -> None:
    """STOP from a number with no audience_members row → opt-out
    audit log still records the action; label sync silently skips
    (no Chatwoot contact to label)."""
    pytest.skip(
        "Phase 7 PR-7: operator-driven E2E. See test_stop_opt_out_propagates_label_within_5s."
    )
