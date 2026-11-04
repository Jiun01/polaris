"""Quota-exhausted escalation — extracted from pipeline.py (Batch E, task E1;
see PRP plan audit-batch-e-structural-splits).

``_escalate_quota_exhausted``: quota over-cap -> holding message to the
customer + hand the Chatwoot conversation to a human. Called once from
``pipeline._run_inner``.

Calls back into pipeline.py's ``_dispatch_reply`` / ``_templated_response``
via a late (function-body) import of the ``pipeline`` module itself — same
pattern as ``pipeline_voice.py``, see that module's docstring. Already does
its own late imports of ``chatwoot.handoff`` / ``chatwoot.label_sync`` /
``chatwoot.client`` internally (unchanged from pipeline.py).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import asyncpg
from polaris_shared_types import IncomingMessage

from polaris_api.config import get_settings

if TYPE_CHECKING:
    # Type-only import — avoids a hard circular import at module-load time.
    # See pipeline_voice.py's docstring / import-block comment for the full
    # explanation (same pattern applies here).
    from polaris_api.services.pipeline import PipelineDeps

logger = logging.getLogger(__name__)

_QUOTA_ESCALATION_IN_HOURS: Final[str] = "Please wait while we get our support agent."


_QUOTA_ESCALATION_AFTER_HOURS: Final[str] = (
    "Thanks for your message! We're outside our hours right now, but a support "
    "agent will get back to you as soon as we're back."
)


async def _escalate_quota_exhausted(
    *,
    conn: asyncpg.Connection,
    deps: PipelineDeps,
    incoming: IncomingMessage,
    channel: str,
    cw_account_id: int | None,
    cw_conv_id: int | None,
) -> None:
    """Quota over-cap → holding message to the customer + escalate to a human.

    Two best-effort side-effects (a failure in either is logged, never raised —
    the inbound webhook contract is always-ack and the customer message + the
    conversation row already exist):

      1. Send a business-hours-aware holding message on the customer's channel
         (direct dispatch, NOT counted as a bot reply).
      2. Hand the Chatwoot conversation to a person — flip status to ``open``
         and apply the ``human-handling`` label — so Chatwoot's native
         auto-assignment / round-robin routes it to an agent. The flip to
         ``open`` also stops the bot re-escalating: the next inbound is gated
         out at the consumer (status != pending).
    """
    from polaris_api.services import pipeline as _pipeline

    # Business-hours selection (reuse the pure handoff helper). A read failure
    # falls through to the after-hours wording rather than crashing the inbound.
    from polaris_api.services.chatwoot import handoff as _handoff

    raw_bh: object = None
    timezone: str | None = None
    try:
        row = await conn.fetchrow(
            "SELECT business_hours, timezone FROM tenants WHERE id = $1",
            incoming.tenant_id,
        )
        if row is not None:
            raw_bh = row["business_hours"]
            tz_val = row["timezone"]
            timezone = tz_val if isinstance(tz_val, str) else None
    except Exception:  # noqa: BLE001 — degrade to after-hours wording
        logger.warning(
            "pipeline.quota_escalation: business-hours read failed tenant=%s",
            incoming.tenant_id,
        )

    in_hours = _handoff.is_in_business_hours(
        # asyncpg returns JSONB as dict OR str depending on the codec — reuse the
        # canonical handoff normaliser rather than re-deriving it here.
        business_hours=_handoff._normalise_jsonb(raw_bh),
        timezone=timezone,
        grace_minutes=get_settings().handoff_business_hours_grace_min,
    )
    text = _QUOTA_ESCALATION_IN_HOURS if in_hours else _QUOTA_ESCALATION_AFTER_HOURS

    # 1) Holding message to the customer (best-effort).
    try:
        await _pipeline._dispatch_reply(
            deps=deps,
            tenant_id=incoming.tenant_id,
            channel=channel,
            channel_user_id=incoming.sender.channel_user_id,
            response=_pipeline._templated_response(text),
            channel_id=incoming.channel_id,
        )
    except Exception:  # noqa: BLE001 — holding message is best-effort
        logger.exception(
            "pipeline.quota_escalation: holding-message send failed tenant=%s",
            incoming.tenant_id,
        )

    # 2) Hand the Chatwoot conversation to a human (status=open + label).
    if cw_account_id is None or cw_conv_id is None:
        return
    try:
        from polaris_api.services.chatwoot import label_sync
        from polaris_api.services.chatwoot.client import (
            get_chatwoot_client as _get_cw_client,
        )

        cw = _get_cw_client()
        await cw.flip_conversation_status(
            account_id=cw_account_id,
            conversation_id=cw_conv_id,
            status="open",
        )
        await label_sync.sync_handling_label(
            account_id=cw_account_id,
            conversation_id=cw_conv_id,
            owner="human",
            chatwoot=cw,
        )
    except Exception:  # noqa: BLE001 — CW escalation is best-effort
        logger.exception(
            "pipeline.quota_escalation: chatwoot escalation failed tenant=%s cw_conv=%s",
            incoming.tenant_id,
            cw_conv_id,
        )
