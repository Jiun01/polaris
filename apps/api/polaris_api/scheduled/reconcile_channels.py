"""Channel reconciliation — periodic self-heal for two failure classes
(silent-failure audit findings #3 and #4, 2026-07-02).

1. Unattached agent-bot: a transient Chatwoot blip during first-inbound
   lazy-provisioning (or a manual channel-add) can leave an inbox with NO
   platform bot attached. Chatwoot never fires ``message_created`` to Polaris
   for such an inbox again — every future message on that channel gets zero
   replies, permanently, until an operator notices and runs
   ``scripts/backfill_agent_bot_attach.py`` by hand. This job runs that same
   idempotent attach logic on a schedule so the self-heal is automatic.

2. Missing channel access-token secret: ``lazy_provision.py``'s best-effort
   token capture (``get_channel_token`` -> ``secrets_svc.put``) can fail
   silently (collapses ALL failures to None) leaving a channel "provisioned"
   with no usable credential -> outbound replies raise ``DispatchSkipped``
   forever. This job cannot re-mint a token itself (Chatwoot's
   ``get_channel_token`` needs the SAME inbox context lazy_provision already
   had), so it logs + counts the gap for operator visibility rather than
   silently existing forever with no trail.

Per-channel error isolation: attach_inbox_agent_bot() NEVER raises (see
services/chatwoot/agent_bot.py docstring) so one unreachable Chatwoot account
is skipped and the batch continues. Uses the service pool (BYPASSRLS) to scan
across every tenant in one query, mirroring ea_settings_sync.py's
fresh-connection-per-tenant-batch shape.

Channel-kind literal note: ``tenant_channels.channel``'s CHECK constraint (as
of migration 109) admits exactly ``whatsapp``, ``messenger``, ``telegram``,
``email``, ``instagram_login`` — the legacy plain ``instagram`` (FB-page)
kind was dropped. ``_CHANNEL_TO_SECRET_KIND`` below keys on
``instagram_login``, matching both the current schema and
``lazy_provision._CHANNEL_TO_SECRET_KIND`` exactly.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import asyncpg

from polaris_api import metrics
from polaris_api.services import secrets as secrets_svc
from polaris_api.services.channels.secret_kinds import BASE_CHANNEL_SECRET_KINDS
from polaris_api.services.chatwoot.agent_bot import (
    attach_inbox_agent_bot,
    resolve_platform_bot_id,
)
from polaris_api.services.chatwoot.client import ChatwootError, get_chatwoot_client

logger = logging.getLogger(__name__)

_scheduler = None

# Polaris channel kind -> tenant_secrets.kind for the captured access token.
# See services.channels.secret_kinds for the shared base map.
_CHANNEL_TO_SECRET_KIND = BASE_CHANNEL_SECRET_KINDS

_DEFAULT_INTERVAL_S = 3600  # hourly, same cadence family as ea_settings_sync


def _interval_seconds() -> int:
    raw = os.getenv("CHANNEL_RECONCILE_INTERVAL_SECONDS")
    if raw is None or not raw.strip():
        return _DEFAULT_INTERVAL_S
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "CHANNEL_RECONCILE_INTERVAL_SECONDS=%r not int; using default %d",
            raw, _DEFAULT_INTERVAL_S,
        )
        return _DEFAULT_INTERVAL_S
    return value if value > 0 else _DEFAULT_INTERVAL_S


async def _fetch_channel_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tc.tenant_id           AS tenant_id,
                   t.chatwoot_account_id  AS account_id,
                   tc.chatwoot_inbox_id   AS inbox_id,
                   tc.channel             AS channel
              FROM tenant_channels tc
              JOIN tenants t ON t.id = tc.tenant_id
             WHERE t.chatwoot_account_id IS NOT NULL
               AND tc.status = 'active'
             ORDER BY t.chatwoot_account_id, tc.chatwoot_inbox_id
            """
        )
    return list(rows)


async def _reconcile_job(pool: asyncpg.Pool) -> None:
    run_start = datetime.now(UTC)
    rows = await _fetch_channel_rows(pool)
    if not rows:
        logger.info("reconcile_channels: no active tenant_channels — nothing to do")
        return

    chatwoot = get_chatwoot_client()
    bots_attached = tokens_missing = checked = 0
    # Resolve the platform bot id ONCE per Chatwoot account, not once per
    # inbox — rows are ORDER BY account_id, mirroring
    # scripts/backfill_agent_bot_attach.py's per-account cache. A failed
    # resolve caches None for the rest of this tick (every inbox on that
    # account counts as attach-failed); the next hourly tick retries.
    bot_id_by_account: dict[int, int | None] = {}

    # Single connection for the whole batch (mirrors quota_flush.py's B7-d
    # acquire-once pattern) instead of one acquire per row.
    async with pool.acquire() as conn:
        for row in rows:
            checked += 1
            account_id = int(row["account_id"])
            inbox_id = int(row["inbox_id"])
            channel = row["channel"]
            tenant_id = row["tenant_id"]

            if account_id not in bot_id_by_account:
                try:
                    bot_id_by_account[account_id] = await resolve_platform_bot_id(
                        chatwoot, account_id=account_id
                    )
                except ChatwootError as exc:
                    logger.warning(
                        "reconcile_channels: bot resolve failed account=%s: %s — "
                        "skipping attach for this account until next tick",
                        account_id,
                        type(exc).__name__,
                    )
                    bot_id_by_account[account_id] = None
            bot_id = bot_id_by_account[account_id]

            # 1. Re-attach the platform bot. Idempotent (set_inbox_agent_bot is
            # find_or_create on Chatwoot's side) and NEVER raises — safe to call
            # unconditionally every tick rather than pre-checking attach state.
            # The resolved bot_id is passed through so the per-account lookup
            # above is the only list_agent_bots call this tick.
            attached = bot_id is not None and await attach_inbox_agent_bot(
                chatwoot, account_id=account_id, inbox_id=inbox_id, bot_id=bot_id
            )
            if attached:
                bots_attached += 1
            else:
                metrics.record_channel_reconcile_fix(
                    tenant_id=tenant_id, kind="agent_bot_attach_failed"
                )

            # 2. Missing access-token secret check (whatsapp/messenger/
            # instagram_login only — telegram/email tokens aren't auto-captured).
            secret_kind = _CHANNEL_TO_SECRET_KIND.get(channel)
            if secret_kind is not None:
                token = await secrets_svc.get(conn, tenant_id, secret_kind)
                if token is None:
                    tokens_missing += 1
                    logger.warning(
                        "reconcile_channels: tenant=%s inbox=%s channel=%s missing "
                        "access-token secret (kind=%s) — outbound sends will raise "
                        "DispatchSkipped until an operator re-provisions",
                        tenant_id, inbox_id, channel, secret_kind,
                    )
                    metrics.record_channel_reconcile_fix(
                        tenant_id=tenant_id, kind="token_missing"
                    )

    elapsed = (datetime.now(UTC) - run_start).total_seconds()
    logger.info(
        "reconcile_channels: done checked=%d bots_attached=%d tokens_missing=%d "
        "elapsed_s=%.1f",
        checked, bots_attached, tokens_missing, elapsed,
    )


def register_reconcile_channels_scheduler(service_pool: asyncpg.Pool) -> None:
    """Register the interval channel-reconciliation job."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning(
            "apscheduler not installed — channel reconciliation scheduler disabled."
        )
        return

    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock

    interval_s = _interval_seconds()

    async def _job() -> None:
        await run_with_advisory_lock(
            service_pool,
            job_name="reconcile_channels",
            lock_key=JOB_LOCK_KEYS["reconcile_channels"],
            job=lambda: _reconcile_job(service_pool),
        )

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _job,
        IntervalTrigger(seconds=interval_s),
        id="reconcile_channels",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=interval_s,
    )
    _scheduler.start()
    logger.info("reconcile_channels: scheduler registered (every %ds)", interval_s)


def shutdown_reconcile_channels_scheduler() -> None:
    """Gracefully stop the channel reconciliation scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
