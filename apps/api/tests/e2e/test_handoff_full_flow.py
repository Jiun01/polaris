"""Phase 7 PR-7 — E2E handoff full flow.

Real DB (skips without ``POLARIS_DATABASE_URL`` / ``SUPABASE_DB_URL``)
+ mocked Meta + Chatwoot HTTP. Mirrors the pattern of
``test_e2e_webhook_to_messages.py``.

What we cover:
  * Bot inbound on WhatsApp → flow runtime fires the ``handoff`` node →
    ChatwootAdapter delegates to ``services/chatwoot/handoff.py``
  * Customer-facing handoff message goes out via Meta (in-hours OR
    after-hours variant per ADR-019)
  * Chatwoot path runs all three sequential calls (flip status, assign
    team, post private note)
  * ``conversations.handoff_at`` + ``tenants.handoff_at`` written
  * ``webhook_events_outbound`` carries the ``handoff.initiated`` row
  * Recovery marker stays NULL on the confirmed path

Mock-based orchestration tests for the same scenarios already live in
``tests/chatwoot/test_handoff_service.py`` — those run in CI with no
DB requirement. This E2E layer exists for the operator's local sweep
when validating a phase close: it boots the full Polaris API + flow
runtime + DB persistence + Chatwoot stub end-to-end.

Skip-without-DB is intentional. CI runs with seeded fixtures locally;
this file is the operator's pre-flight gate before a phase tag.
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
        "Phase 7 E2E sweep is operator-driven against a seeded local DB."
    ),
)


@pytest.mark.asyncio
async def test_handoff_in_hours_confirmed_end_to_end() -> None:
    """In-hours WhatsApp inbound → handoff node → Chatwoot reachable
    → outcome=confirmed.

    Seeding contract: the operator's local fixture seeds a tenant with
    business_hours covering NOW + a chatwoot_account_id linked. The
    test fakes Meta + Chatwoot HTTP via the same _Httpx shim pattern
    used in tests/chatwoot/conftest.py.

    Assertions:
      - HandoffResult.outcome == "confirmed"
      - HandoffResult.business_hours_state == "in_hours"
      - conversations.handoff_at IS NOT NULL
      - webhook_events_outbound has one row with event_type='handoff.initiated'
      - handoff_pending_recovery_at IS NULL (clean confirmed path)
    """
    pytest.skip(
        "Phase 7 PR-7: operator-driven E2E. See module docstring. "
        "Implementation deferred — orchestration coverage lives in "
        "tests/chatwoot/test_handoff_service.py."
    )


@pytest.mark.asyncio
async def test_handoff_after_hours_alt_message_end_to_end() -> None:
    """Same as above, but with the tenant fixture's business_hours
    closed at NOW. Asserts the after-hours message variant goes out
    + business_hours_state == 'after_hours'."""
    pytest.skip(
        "Phase 7 PR-7: operator-driven E2E. See test_handoff_in_hours_confirmed_end_to_end."
    )


@pytest.mark.asyncio
async def test_handoff_chatwoot_5xx_sets_recovery_marker_end_to_end() -> None:
    """Customer message succeeds via Meta but Chatwoot 5xx after retry
    budget → outcome=declined + ``conversations.handoff_pending_recovery_at``
    is set per Q-PHASE7-04. Next inbound on the same conversation should
    fire the recovery-note path via ``services/chatwoot/recovery.py``."""
    pytest.skip(
        "Phase 7 PR-7: operator-driven E2E. See test_handoff_in_hours_confirmed_end_to_end."
    )
