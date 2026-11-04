"""Step 5 (Chatwoot-native) — quota-exhausted graceful escalation.

When the bot reply cap is hit, the pipeline must:
  1. send the customer a business-hours-aware holding message, and
  2. hand the Chatwoot conversation to a human (status=open + human-handling
     label) so native auto-assignment / round-robin routes it to an agent,

instead of silently dropping the reply. Both side-effects are best-effort and
must never raise (the inbound webhook contract is always-ack).

These unit-test ``pipeline_quota_escalation._escalate_quota_exhausted`` directly
(extracted from pipeline.py in Batch E, task E1); the
over-cap → escalation wiring is covered in ``test_pipeline_pr1``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from polaris_shared_types.incoming_message import IncomingMessage, SenderRef


class _Conn:
    """Minimal asyncpg.Connection stand-in for the business-hours read."""

    def __init__(self, business_hours: Any = None, timezone: str = "UTC") -> None:
        self._bh = business_hours
        self._tz = timezone

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "business_hours, timezone" in sql:
            return {"business_hours": self._bh, "timezone": self._tz}
        return None


def _incoming(tenant_id: UUID, channel_user_id: str = "user-001") -> IncomingMessage:
    return IncomingMessage(
        tenant_id=tenant_id,
        channel_id=uuid4(),
        provider="whatsapp",
        provider_message_id="wamid-x",
        body="hello",
        sender=SenderRef(channel_user_id=channel_user_id),
        received_at=datetime.now(UTC),
    )


def _deps() -> Any:
    from polaris_api.services.meta_whatsapp import MetaWhatsAppClient
    from polaris_api.services.pipeline import PipelineDeps

    return PipelineDeps(
        anon_pool=MagicMock(),
        redis=AsyncMock(),
        meta_client=MagicMock(spec=MetaWhatsAppClient),
    )


async def test_in_hours_sends_please_wait_and_escalates_cw() -> None:
    """In-hours: holding message uses the in-hours wording + CW is escalated."""
    from polaris_api.services import pipeline
    from polaris_api.services import pipeline_quota_escalation as quota_escalation

    cw = AsyncMock()
    with (
        patch.object(pipeline, "_dispatch_reply", new=AsyncMock(return_value="prov-1")) as disp,
        patch(
            "polaris_api.services.chatwoot.handoff.is_in_business_hours",
            return_value=True,
        ),
        patch(
            "polaris_api.services.chatwoot.client.get_chatwoot_client",
            return_value=cw,
        ),
        patch(
            "polaris_api.services.chatwoot.label_sync.sync_handling_label",
            new=AsyncMock(return_value=True),
        ) as label,
    ):
        await quota_escalation._escalate_quota_exhausted(
            conn=_Conn(),
            deps=_deps(),
            incoming=_incoming(uuid4()),
            channel="whatsapp",
            cw_account_id=7,
            cw_conv_id=99,
        )

    disp.assert_awaited_once()
    assert disp.await_args.kwargs["response"].content == (
        quota_escalation._QUOTA_ESCALATION_IN_HOURS
    )
    cw.flip_conversation_status.assert_awaited_once_with(
        account_id=7, conversation_id=99, status="open"
    )
    label.assert_awaited_once()
    assert label.await_args.kwargs["owner"] == "human"


async def test_after_hours_uses_after_hours_wording() -> None:
    from polaris_api.services import pipeline
    from polaris_api.services import pipeline_quota_escalation as quota_escalation

    cw = AsyncMock()
    with (
        patch.object(pipeline, "_dispatch_reply", new=AsyncMock(return_value=None)) as disp,
        patch(
            "polaris_api.services.chatwoot.handoff.is_in_business_hours",
            return_value=False,
        ),
        patch(
            "polaris_api.services.chatwoot.client.get_chatwoot_client",
            return_value=cw,
        ),
        patch(
            "polaris_api.services.chatwoot.label_sync.sync_handling_label",
            new=AsyncMock(),
        ),
    ):
        await quota_escalation._escalate_quota_exhausted(
            conn=_Conn(),
            deps=_deps(),
            incoming=_incoming(uuid4()),
            channel="whatsapp",
            cw_account_id=1,
            cw_conv_id=2,
        )

    assert disp.await_args.kwargs["response"].content == (
        quota_escalation._QUOTA_ESCALATION_AFTER_HOURS
    )


async def test_no_chatwoot_link_still_sends_holding_message() -> None:
    """No CW conversation → still sends the message, never touches the CW API."""
    from polaris_api.services import pipeline
    from polaris_api.services import pipeline_quota_escalation as quota_escalation

    with (
        patch.object(pipeline, "_dispatch_reply", new=AsyncMock(return_value="p")) as disp,
        patch(
            "polaris_api.services.chatwoot.handoff.is_in_business_hours",
            return_value=True,
        ),
        patch("polaris_api.services.chatwoot.client.get_chatwoot_client") as cw_factory,
        patch(
            "polaris_api.services.chatwoot.label_sync.sync_handling_label",
            new=AsyncMock(),
        ) as label,
    ):
        await quota_escalation._escalate_quota_exhausted(
            conn=_Conn(),
            deps=_deps(),
            incoming=_incoming(uuid4()),
            channel="whatsapp",
            cw_account_id=None,
            cw_conv_id=None,
        )

    disp.assert_awaited_once()
    cw_factory.assert_not_called()
    label.assert_not_awaited()


async def test_business_hours_read_failure_degrades_to_after_hours() -> None:
    """A DB error reading business hours must not raise — degrade to after-hours
    wording and still dispatch the holding message."""
    from polaris_api.services import pipeline
    from polaris_api.services import pipeline_quota_escalation as quota_escalation

    class _ErrorConn(_Conn):
        async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
            raise RuntimeError("DB unavailable")

    cw = AsyncMock()
    with (
        patch.object(pipeline, "_dispatch_reply", new=AsyncMock(return_value="p")) as disp,
        patch(
            "polaris_api.services.chatwoot.client.get_chatwoot_client",
            return_value=cw,
        ),
        patch(
            "polaris_api.services.chatwoot.label_sync.sync_handling_label",
            new=AsyncMock(),
        ),
    ):
        # Must NOT raise; the real is_in_business_hours runs on an empty schedule
        # (normalised from None) → after-hours.
        await quota_escalation._escalate_quota_exhausted(
            conn=_ErrorConn(),
            deps=_deps(),
            incoming=_incoming(uuid4()),
            channel="whatsapp",
            cw_account_id=1,
            cw_conv_id=2,
        )

    disp.assert_awaited_once()
    assert disp.await_args.kwargs["response"].content == (
        quota_escalation._QUOTA_ESCALATION_AFTER_HOURS
    )


async def test_chatwoot_failure_is_best_effort() -> None:
    """A Chatwoot escalation error is swallowed — the inbound must not fail."""
    from polaris_api.services import pipeline
    from polaris_api.services import pipeline_quota_escalation as quota_escalation

    cw = AsyncMock()
    cw.flip_conversation_status.side_effect = RuntimeError("chatwoot down")
    with (
        patch.object(pipeline, "_dispatch_reply", new=AsyncMock(return_value="p")) as disp,
        patch(
            "polaris_api.services.chatwoot.handoff.is_in_business_hours",
            return_value=True,
        ),
        patch(
            "polaris_api.services.chatwoot.client.get_chatwoot_client",
            return_value=cw,
        ),
        patch(
            "polaris_api.services.chatwoot.label_sync.sync_handling_label",
            new=AsyncMock(),
        ),
    ):
        # Must NOT raise.
        await quota_escalation._escalate_quota_exhausted(
            conn=_Conn(),
            deps=_deps(),
            incoming=_incoming(uuid4()),
            channel="whatsapp",
            cw_account_id=1,
            cw_conv_id=2,
        )

    disp.assert_awaited_once()
