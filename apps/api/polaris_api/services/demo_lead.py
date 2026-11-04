"""Demo lead flagging — Demo System v3 (ADR-066).

When a demo ends (message cap or time expiry) the contact is a warm lead. We
flag it two ways:
  1. stamp ``demo_sessions.lead_flagged_at`` (the Demo System log marker the
     admin "leads" view reads), and
  2. post a Chatwoot **private note** to the contact's conversation so the
     operator sees the lead in their day-to-day inbox.

Best-effort: a Chatwoot failure must not crash the inbound turn — the customer
already received the "demo ended" message and reverts to the Polaris sales bot.
The caller supplies the Chatwoot account/conversation ids from the live inbound
context (``IncomingMessage.chatwoot_account_id`` / ``chatwoot_conversation_id``),
since a session row activated earlier may not have captured them.
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from polaris_api.services import demo_sessions

logger = logging.getLogger(__name__)

_NOTE_TEMPLATE = (
    "\U0001f3af Demo lead — the {brand} demo just ended for this contact "
    "(they used their full demo). Good moment to follow up about Polaris."
)


class _CwNotePoster(Protocol):
    async def post_private_note(
        self, *, account_id: int, conversation_id: int, content: str
    ) -> int: ...


async def flag(
    *,
    session_id: UUID,
    brand_name: str,
    cw_client: _CwNotePoster | None,
    cw_account_id: int | None,
    cw_conversation_id: int | None,
) -> None:
    """Stamp the lead marker and (best-effort) post a Chatwoot private note."""
    # 1. Always stamp the Demo System log marker.
    try:
        await demo_sessions.mark_lead_flagged(session_id)
    except Exception:  # noqa: BLE001 — never crash the turn on a logging write
        logger.warning(
            "demo_lead: failed to stamp lead_flagged_at session=%s", session_id
        )

    # 2. Best-effort Chatwoot private note (skip if no CW link this turn).
    if cw_client is None or cw_account_id is None or cw_conversation_id is None:
        return
    try:
        await cw_client.post_private_note(
            account_id=cw_account_id,
            conversation_id=cw_conversation_id,
            content=_NOTE_TEMPLATE.format(brand=brand_name),
        )
    except Exception:  # noqa: BLE001 — CW failure must not crash the inbound turn
        logger.warning(
            "demo_lead: failed to post Chatwoot private note conv=%s",
            cw_conversation_id,
        )
