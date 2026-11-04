"""Backfill the retrieval tools onto existing agent-enabled tenants (WS2).

New tenants get ``lookup_products`` + ``lookup_promotions`` in their default
``enabled_tools`` at provisioning. This backfills tenants created BEFORE that
change so their bots also surface product/promo answers. Additive only — it
never removes an operator's customized tools, and it's idempotent.

No cache bust needed: ``services/tenant_snapshot.load_tenant_snapshot`` reads
``tenants.settings`` directly per inbound turn (no Redis cache), so a change
takes effect on the tenant's next message.

Run::

    uv run python apps/api/scripts/backfill_default_agent_tools.py [--apply]

Without ``--apply`` it is a dry run (prints what WOULD change). Env (auto-loaded
from repo-root .env): POLARIS_DB_URL_SERVICE (or DATABASE_URL / SUPABASE_DB_URL).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

sys.path.insert(0, str(Path(__file__).parents[2]))

import asyncpg  # noqa: E402
from polaris_shared_types import DEFAULT_RETRIEVAL_TOOLS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_default_agent_tools")


def _dsn() -> str:
    dsn = (
        os.environ.get("POLARIS_DB_URL_SERVICE")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
    )
    if not dsn:
        raise SystemExit("No DB DSN: set POLARIS_DB_URL_SERVICE (or DATABASE_URL / SUPABASE_DB_URL).")
    return dsn


async def main(apply: bool) -> None:
    conn = await asyncpg.connect(_dsn())
    changed = 0
    try:
        rows = await conn.fetch(
            """
            SELECT id, settings->'agent_config' AS agent_config
              FROM tenants
             WHERE settings->'agent_config'->>'enabled' = 'true'
            """
        )
        for row in rows:
            cfg = row["agent_config"]
            cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
            tools: list[str] = list(cfg.get("enabled_tools") or [])
            missing = [t for t in DEFAULT_RETRIEVAL_TOOLS if t not in tools]
            if not missing:
                continue
            new_tools = tools + missing
            changed += 1
            logger.info("tenant=%s: %s -> %s", row["id"], tools, new_tools)
            if apply:
                await conn.execute(
                    """
                    UPDATE tenants
                       SET settings = jsonb_set(
                             settings, '{agent_config,enabled_tools}', $2::jsonb
                           )
                     WHERE id = $1
                    """,
                    row["id"],
                    json.dumps(new_tools),
                )
    finally:
        await conn.close()
    verb = "updated" if apply else "would update"
    logger.info("%s %d agent-enabled tenant(s)%s", verb, changed, "" if apply else " (dry run — pass --apply)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
