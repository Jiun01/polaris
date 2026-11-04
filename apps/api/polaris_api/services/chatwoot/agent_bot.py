"""Agent-bot auto-attach — Chatwoot-native rehaul (Stage 3).

Promotes the manual ``seed_e2e_chatwoot_bots.py`` step to runtime. When a tenant
channel/inbox is provisioned, attach the shared platform 'Polaris Bot' so new
conversations start in ``pending`` (bot-owned) — which is what makes the
human-handling gate + native escalation work. The platform bot is seeded
out-of-band (one bot, ``account_id IS NULL``, accessible to every account); here
we only resolve its id by name and link it to the inbox.

Best-effort by design: a Chatwoot outage must never fail channel provisioning.
The caller (admin channel-add) runs this AFTER the DB transaction commits, so a
failure leaves a working channel with an unattached inbox that the backfill
script ``scripts/backfill_agent_bot_attach.py`` (or a re-add) can fix.
"""

from __future__ import annotations

import logging

from polaris_api.services.chatwoot.client import (
    ChatwootClient,
    ChatwootConfigError,
    ChatwootError,
)

logger = logging.getLogger(__name__)

# Name of the shared platform bot seeded by seed_e2e_chatwoot_bots.py.
PLATFORM_BOT_NAME: str = "Polaris Bot"


async def resolve_platform_bot_id(
    chatwoot: ChatwootClient,
    *,
    account_id: int,
    bot_name: str = PLATFORM_BOT_NAME,
) -> int | None:
    """Return the id of the shared platform bot visible to ``account_id``.

    Looks it up by name via ``list_agent_bots`` (the account scope includes
    platform bots). Returns ``None`` when no bot of that name is found — the
    caller treats that as "not attachable" and logs.
    """
    bots = await chatwoot.list_agent_bots(account_id=account_id)
    for bot in bots:
        if isinstance(bot, dict) and bot.get("name") == bot_name:
            bot_id = bot.get("id")
            if isinstance(bot_id, int):
                return bot_id
    return None


async def attach_inbox_agent_bot(
    chatwoot: ChatwootClient,
    *,
    account_id: int,
    inbox_id: int,
    bot_name: str = PLATFORM_BOT_NAME,
    bot_id: int | None = None,
) -> bool:
    """Attach the shared platform bot to ``inbox_id``. Best-effort.

    Returns ``True`` on a confirmed attach, ``False`` on any failure (bot not
    found, or a Chatwoot config/structural/transient error). NEVER raises —
    provisioning must not fail because the inbox couldn't be linked to the bot.

    Pass ``bot_id`` when the caller already resolved it to skip the per-call
    ``list_agent_bots`` lookup — batch callers resolve once per account
    (``scheduled/reconcile_channels.py``, ``scripts/backfill_agent_bot_attach.py``).
    """
    try:
        if bot_id is None:
            bot_id = await resolve_platform_bot_id(
                chatwoot, account_id=account_id, bot_name=bot_name
            )
        if bot_id is None:
            logger.warning(
                "agent_bot attach: platform bot %r not found in account=%s — "
                "inbox=%s left unattached (run the backfill script)",
                bot_name,
                account_id,
                inbox_id,
            )
            return False
        await chatwoot.set_inbox_agent_bot(
            account_id=account_id, inbox_id=inbox_id, agent_bot_id=bot_id
        )
        logger.info(
            "agent_bot attach: bot=%s -> account=%s inbox=%s",
            bot_id,
            account_id,
            inbox_id,
        )
        return True
    except ChatwootConfigError as exc:
        # Permanent deploy misconfiguration (e.g. CHATWOOT_AGENT_API_TOKEN unset),
        # NOT a transient Chatwoot outage. Log at ERROR with the message so it's
        # distinguishable from a blip — otherwise every channel-add silently
        # leaves inboxes unattached forever. Still non-fatal: a missing token must
        # not 500 the channel-add; ops fixes config + re-runs the backfill.
        logger.error(
            "agent_bot attach: Chatwoot not configured (%s) — account=%s "
            "inbox=%s NOT attached; fix config + run "
            "scripts/backfill_agent_bot_attach.py",
            exc,
            account_id,
            inbox_id,
        )
        return False
    except ChatwootError as exc:
        logger.warning(
            "agent_bot attach failed account=%s inbox=%s: %s",
            account_id,
            inbox_id,
            type(exc).__name__,
        )
        return False
