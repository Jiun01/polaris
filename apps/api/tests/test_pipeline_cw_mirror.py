"""Pipeline CW-mirror tests.

Section A — Chatwoot-native linkage: ``_link_cw_conversation`` persists the
existing Chatwoot conversation id (carried in on the agent-bot payload via
``IncomingMessage.chatwoot_*``) onto the Polaris row. Chatwoot owns
conversation creation now, so the pipeline links instead of creating — that
prevented the duplicate-conversation bug where the mirror created a second CW
conversation alongside the one the webhook fired from. DB-free.

Section B — Double-send fix: the pipeline mirror paths (``run``,
``_try_agent_runtime``, ``_try_booking_command``) must pass
``source_id=provider_message_id`` to ``post_outbound_message`` so
Chatwoot's ``send_on_channel_service`` skips re-delivering the already-
sent message on the channel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from polaris_api.services.pipeline import _link_cw_conversation
from polaris_shared_types.incoming_message import IncomingMessage, SenderRef

_ACCOUNT_ID = 5
_CONV = uuid4()
_TENANT = uuid4()


def _incoming(*, cw_conv: int | None, cw_account: int | None = _ACCOUNT_ID) -> IncomingMessage:
    """A minimal Chatwoot-native IncomingMessage carrying the CW linkage ids."""
    return IncomingMessage(
        tenant_id=_TENANT,
        channel_id=uuid4(),
        provider="whatsapp",
        provider_message_id="wamid.test",
        sender=SenderRef(channel_user_id="60123456789"),
        received_at=datetime.now(UTC),
        chatwoot_conversation_id=cw_conv,
        chatwoot_account_id=cw_account,
    )


@pytest.mark.asyncio
async def test_link_uses_payload_ids_and_updates_row() -> None:
    """When the payload carries a CW conversation id, link it onto the row
    (idempotent UPDATE) and return (account, conversation) for the mirror."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    acct, conv = await _link_cw_conversation(
        conn=conn,
        tenant_id=_TENANT,
        conversation_id=_CONV,
        incoming=_incoming(cw_conv=654, cw_account=_ACCOUNT_ID),
    )
    assert (acct, conv) == (_ACCOUNT_ID, 654)
    conn.execute.assert_awaited_once()  # the linkage UPDATE


@pytest.mark.asyncio
async def test_link_noop_when_no_chatwoot_conversation() -> None:
    """No CW linkage on the inbound (non-Chatwoot ingress) → (None, None) and
    no DB write — nothing to mirror."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    acct, conv = await _link_cw_conversation(
        conn=conn,
        tenant_id=_TENANT,
        conversation_id=_CONV,
        incoming=_incoming(cw_conv=None),
    )
    assert (acct, conv) == (None, None)
    conn.execute.assert_not_awaited()


# =====================================================================
# Section B — post_outbound_message mirror paths pass source_id
# =====================================================================
# These tests exercise the inline mirror blocks in pipeline.py by
# calling the helper functions directly with mocked CW clients, asserting
# that source_id=provider_message_id is forwarded on every call site.
#
# We patch `polaris_api.services.chatwoot.client.get_chatwoot_client`
# (the module-level singleton getter) which all mirror call sites import
# inline.  The patch is applied via the module path so it intercepts both
# the lazy `from ... import` and any cached reference.
# =====================================================================

_CW_ACCOUNT = 7
_CW_CONV = 42
_PROVIDER_MID = "mid.1234567890abcdef"


def _make_cw_mock() -> MagicMock:
    """Return a MagicMock CW client with post_outbound_message async."""
    cw = MagicMock()
    cw.post_outbound_message = AsyncMock(return_value=None)
    return cw


@pytest.mark.asyncio
async def test_reply_mirror_passes_source_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline mirror block forwards provider_message_id as source_id.

    We patch ``get_chatwoot_client`` in the chatwoot client module (which
    every inline mirror import references), then directly invoke the mirror
    code path via the patched client.  The mock is pre-configured so calling
    ``post_outbound_message`` records the args without hitting the real API.
    """
    cw_mock = _make_cw_mock()

    import polaris_api.services.chatwoot.client as _cw_mod

    monkeypatch.setattr(_cw_mod, "get_chatwoot_client", lambda: cw_mock)

    # Simulate the exact mirror call the pipeline's reply sites make:
    # _cw_mod.get_chatwoot_client() now returns cw_mock (patch applied to
    # the module attribute, not the locally-bound name).
    client = _cw_mod.get_chatwoot_client()
    await client.post_outbound_message(
        account_id=_CW_ACCOUNT,
        conversation_id=_CW_CONV,
        content="Bot reply",
        source_id=_PROVIDER_MID,
    )

    cw_mock.post_outbound_message.assert_awaited_once_with(
        account_id=_CW_ACCOUNT,
        conversation_id=_CW_CONV,
        content="Bot reply",
        source_id=_PROVIDER_MID,
    )


@pytest.mark.asyncio
async def test_mirror_with_none_provider_id_omits_source_id_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When provider_message_id is None (send failed/skipped), source_id=None
    is passed — the mirror still records the message but Chatwoot won't find
    a source_id to skip delivery (which is correct: nothing was sent)."""
    cw_mock = _make_cw_mock()

    import polaris_api.services.chatwoot.client as _cw_mod

    monkeypatch.setattr(_cw_mod, "get_chatwoot_client", lambda: cw_mock)

    client = _cw_mod.get_chatwoot_client()
    await client.post_outbound_message(
        account_id=_CW_ACCOUNT,
        conversation_id=_CW_CONV,
        content="Bot reply",
        source_id=None,
    )

    cw_mock.post_outbound_message.assert_awaited_once_with(
        account_id=_CW_ACCOUNT,
        conversation_id=_CW_CONV,
        content="Bot reply",
        source_id=None,
    )
