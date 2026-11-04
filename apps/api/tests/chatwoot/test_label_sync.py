"""Label sync tests (Phase 7 PR-4) — TDD-RED stage.

These tests are written BEFORE services/chatwoot/label_sync.py exists.
Initial run must fail with ImportError to confirm the RED gate per
the local TDD workflow.

Coverage targets per Phase 7 PR-4 checklist:

  - sync_opt_out_label happy path: applies pol_opted_out to the right
    Chatwoot contact under the right account_id
  - sync_opt_out_label idempotency: repeat call doesn't double-apply
    (relies on Chatwoot's label-apply server-side idempotency + our
    ensure_label race handling)
  - sync_opt_out_label with NULL chatwoot_contact_id: silent skip,
    no Chatwoot call attempted
  - sync_opt_out_label with Chatwoot failure: non-blocking — function
    returns False, never raises (eventual-consistency posture, marker
    not blocked from being set in our DB)
  - sync_suspension_label scans all open conversations for a tenant
    that have a chatwoot_conversation_id and applies the label
  - sync_suspension_label skips conversations with NULL chatwoot_conv_id
  - sync_suspension_label is idempotent (re-fire applies same label
    again — Chatwoot side handles dedup)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from polaris_api.services.chatwoot.label_sync import (
    sync_opt_out_label,
    sync_suspension_label,
)


@pytest.fixture
def fake_conn() -> Any:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    return conn


@pytest.fixture
def fake_chatwoot() -> Any:
    cw = MagicMock()
    cw.apply_contact_label = AsyncMock(return_value=None)
    cw.apply_conversation_label = AsyncMock(return_value=None)
    return cw


# =====================================================================
# sync_opt_out_label
# =====================================================================


@pytest.mark.asyncio
async def test_opt_out_label_applied_to_chatwoot_contact(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """Happy path: an audience_member with a chatwoot_contact_id linked
    gets the pol_opted_out label applied to that contact under the
    tenant's chatwoot_account_id."""
    fake_conn.fetchrow.return_value = {
        "chatwoot_contact_id": 12345,
        "chatwoot_account_id": 7,
    }

    result = await sync_opt_out_label(
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123456789",
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert result is True
    fake_chatwoot.apply_contact_label.assert_awaited_once()
    call = fake_chatwoot.apply_contact_label.await_args.kwargs
    assert call["account_id"] == 7
    assert call["contact_id"] == 12345
    assert call["label"] == "pol_opted_out"


@pytest.mark.asyncio
async def test_opt_out_label_silent_skip_when_contact_id_null(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """Not all audience_members are Chatwoot-linked (e.g. CSV-imported
    contacts that never landed in a Chatwoot conversation). Skip
    silently — opt_out is still recorded in our DB, label is best-effort."""
    fake_conn.fetchrow.return_value = {
        "chatwoot_contact_id": None,
        "chatwoot_account_id": 7,
    }

    result = await sync_opt_out_label(
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123456789",
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert result is False
    fake_chatwoot.apply_contact_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_opt_out_label_silent_skip_when_tenant_not_chatwoot_linked(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """Tenant without chatwoot_account_id (e.g. demo tenants that don't
    use Chatwoot) — opt_out still works; label sync skips."""
    fake_conn.fetchrow.return_value = {
        "chatwoot_contact_id": 12345,
        "chatwoot_account_id": None,
    }

    result = await sync_opt_out_label(
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123456789",
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert result is False
    fake_chatwoot.apply_contact_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_opt_out_label_silent_skip_when_member_not_found(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """STOP keyword from a number that's never been an audience_member —
    no row to look up. Skip silently."""
    fake_conn.fetchrow.return_value = None

    result = await sync_opt_out_label(
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60999999999",
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert result is False
    fake_chatwoot.apply_contact_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_opt_out_label_non_blocking_on_chatwoot_failure(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """Chatwoot transient or structural failure must NOT raise back to
    the caller — the opt_out write in Polaris DB is the source of
    truth; label sync is best-effort eventual consistency."""
    from polaris_api.services.chatwoot.client import ChatwootTransientError

    fake_conn.fetchrow.return_value = {
        "chatwoot_contact_id": 12345,
        "chatwoot_account_id": 7,
    }
    fake_chatwoot.apply_contact_label.side_effect = ChatwootTransientError(
        "5xx after retries"
    )

    # Must NOT raise.
    result = await sync_opt_out_label(
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123456789",
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert result is False


@pytest.mark.asyncio
async def test_opt_out_label_skipped_when_chatwoot_client_none(
    fake_conn: Any,
) -> None:
    """Test / simulator mode — no chatwoot client. Skip silently."""
    result = await sync_opt_out_label(
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123456789",
        conn=fake_conn,
        chatwoot=None,
    )
    assert result is False


# =====================================================================
# sync_suspension_label
# =====================================================================


@pytest.mark.asyncio
async def test_suspension_label_applies_to_all_open_conversations(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """A tenant suspension triggers a one-shot scan that applies
    pol_bot_suspended to every open conversation that has a
    chatwoot_conversation_id linked."""
    fake_conn.fetchrow.return_value = {"chatwoot_account_id": 7}
    fake_conn.fetch.return_value = [
        {"chatwoot_conversation_id": 100},
        {"chatwoot_conversation_id": 101},
        {"chatwoot_conversation_id": 102},
    ]

    applied = await sync_suspension_label(
        tenant_id=uuid4(),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert applied == 3
    assert fake_chatwoot.apply_conversation_label.await_count == 3
    # All applications used the same account_id and label.
    for call in fake_chatwoot.apply_conversation_label.await_args_list:
        assert call.kwargs["account_id"] == 7
        assert call.kwargs["label"] == "pol_bot_suspended"


@pytest.mark.asyncio
async def test_suspension_label_skips_conversations_without_chatwoot_link(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """Conversations with NULL chatwoot_conversation_id are skipped —
    no way to apply a label to a Chatwoot conversation that doesn't
    exist yet."""
    fake_conn.fetchrow.return_value = {"chatwoot_account_id": 7}
    # The query already filters out NULL chatwoot_conversation_id at the
    # SQL level — fake.fetch returns ONLY the rows that have it set.
    fake_conn.fetch.return_value = [{"chatwoot_conversation_id": 100}]

    applied = await sync_suspension_label(
        tenant_id=uuid4(),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert applied == 1


@pytest.mark.asyncio
async def test_suspension_label_skips_when_tenant_not_chatwoot_linked(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    fake_conn.fetchrow.return_value = {"chatwoot_account_id": None}

    applied = await sync_suspension_label(
        tenant_id=uuid4(),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert applied == 0
    fake_chatwoot.apply_conversation_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_suspension_label_partial_failure_continues_loop(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """If one conversation's label apply fails, the scan continues
    with the rest. Partial application is intentional — better to
    label most conversations than to bail on the first failure."""
    from polaris_api.services.chatwoot.client import ChatwootStructuralError

    fake_conn.fetchrow.return_value = {"chatwoot_account_id": 7}
    fake_conn.fetch.return_value = [
        {"chatwoot_conversation_id": 100},
        {"chatwoot_conversation_id": 101},
        {"chatwoot_conversation_id": 102},
    ]
    # Middle conversation fails (e.g. already deleted in Chatwoot).
    fake_chatwoot.apply_conversation_label.side_effect = [
        None,
        ChatwootStructuralError("Conversation not found", status_code=404),
        None,
    ]

    applied = await sync_suspension_label(
        tenant_id=uuid4(),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert applied == 2  # First + third
    assert fake_chatwoot.apply_conversation_label.await_count == 3


@pytest.mark.asyncio
async def test_suspension_label_idempotent_on_refire(
    fake_conn: Any, fake_chatwoot: Any
) -> None:
    """Re-firing sync_suspension_label after a brief pause re-applies
    labels — Chatwoot side handles dedup (apply_conversation_label is
    a 200 no-op when the label is already there)."""
    fake_conn.fetchrow.return_value = {"chatwoot_account_id": 7}
    fake_conn.fetch.return_value = [{"chatwoot_conversation_id": 100}]

    first = await sync_suspension_label(
        tenant_id=uuid4(),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )
    second = await sync_suspension_label(
        tenant_id=uuid4(),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
    )

    assert first == 1
    assert second == 1
    assert fake_chatwoot.apply_conversation_label.await_count == 2
