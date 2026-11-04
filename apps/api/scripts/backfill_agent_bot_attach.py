"""Backfill: attach the shared platform 'Polaris Bot' to every tenant inbox.

One-off reconcile for the agent-bot auto-attach (Chatwoot-native rehaul Stage 3).
New channel-adds attach the bot automatically; this fixes inboxes provisioned
before that, or any the best-effort attach missed.

    uv run python apps/api/scripts/backfill_agent_bot_attach.py [--dry-run]

Reads ``(account_id, inbox_id)`` from ``tenant_channels`` JOIN ``tenants``,
resolves the platform bot id per account, and links it via the Chatwoot REST
API. Idempotent — re-running is safe (``set_agent_bot`` is find_or_create).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

sys.path.insert(0, str(Path(__file__).parents[2]))

from polaris_api.services.chatwoot.agent_bot import (  # noqa: E402
    resolve_platform_bot_id,
)
from polaris_api.services.chatwoot.client import get_chatwoot_client  # noqa: E402

logger = logging.getLogger(__name__)


def _service_dsn() -> str:
    dsn = os.environ.get("POLARIS_DB_URL_SERVICE") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("Set POLARIS_DB_URL_SERVICE or SUPABASE_DB_URL in .env.")
    return dsn


async def _fetch_inboxes() -> list[asyncpg.Record]:
    conn: asyncpg.Connection = await asyncpg.connect(_service_dsn())
    try:
        rows: list[asyncpg.Record] = await conn.fetch(
            """
            SELECT t.chatwoot_account_id AS account_id,
                   tc.chatwoot_inbox_id  AS inbox_id,
                   tc.channel            AS channel
              FROM tenant_channels tc
              JOIN tenants t ON t.id = tc.tenant_id
             WHERE t.chatwoot_account_id IS NOT NULL
               AND tc.chatwoot_inbox_id IS NOT NULL
             ORDER BY t.chatwoot_account_id, tc.chatwoot_inbox_id
            """
        )
        return rows
    finally:
        await conn.close()


async def _run(dry_run: bool) -> int:
    rows = await _fetch_inboxes()
    if not rows:
        logger.info("no tenant_channels with a chatwoot inbox — nothing to do")
        return 0

    chatwoot = get_chatwoot_client()
    # The platform bot id is stable per account for the whole run — resolve once
    # per account, not once per inbox (rows are ORDER BY account_id).
    bot_id_cache: dict[int, int | None] = {}
    ok = failed = 0
    for row in rows:
        account_id = int(row["account_id"])
        inbox_id = int(row["inbox_id"])
        if account_id not in bot_id_cache:
            bot_id_cache[account_id] = await resolve_platform_bot_id(
                chatwoot, account_id=account_id
            )
        bot_id = bot_id_cache[account_id]
        if dry_run:
            logger.info(
                "[dry-run] account=%s inbox=%s channel=%s resolved_bot_id=%s",
                account_id,
                inbox_id,
                row["channel"],
                bot_id,
            )
            continue
        if bot_id is None:
            logger.warning(
                "account=%s inbox=%s: platform bot not found — skipped",
                account_id,
                inbox_id,
            )
            failed += 1
            continue
        try:
            await chatwoot.set_inbox_agent_bot(
                account_id=account_id, inbox_id=inbox_id, agent_bot_id=bot_id
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001 — best-effort per inbox
            logger.warning(
                "account=%s inbox=%s: attach failed (%s)",
                account_id,
                inbox_id,
                type(exc).__name__,
            )
            failed += 1

    if dry_run:
        return 0
    logger.info("agent-bot backfill done: attached=%d failed=%d", ok, failed)
    return 1 if failed else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve bot ids and report, without attaching",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
