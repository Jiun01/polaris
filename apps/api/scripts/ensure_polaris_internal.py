"""Idempotently (re)create the polaris-internal demo gateway tenant.

Recovery for the fixed-UUID gateway (migration 008b row + 105 persona + 114
``is_demo``) after it was deleted or overwritten — historically by the E2E
fixture that shared its UUID (``00000000-0000-0000-0000-000000000001``). Safe to
re-run: the persona is only restored when currently empty, so a portal edit is
never clobbered.

Run::

    uv run python apps/api/scripts/ensure_polaris_internal.py

Env (auto-loaded from repo-root .env):
    POLARIS_DB_URL_SERVICE  (or DATABASE_URL / SUPABASE_DB_URL) — BYPASSRLS service DSN
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

sys.path.insert(0, str(Path(__file__).parents[2]))

import asyncpg  # noqa: E402
from polaris_api.services.demo_sessions import (  # noqa: E402
    ensure_polaris_internal_gateway,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ensure_polaris_internal")


def _dsn() -> str:
    dsn = (
        os.environ.get("POLARIS_DB_URL_SERVICE")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
    )
    if not dsn:
        raise SystemExit(
            "No DB DSN found — set POLARIS_DB_URL_SERVICE (or DATABASE_URL / SUPABASE_DB_URL)."
        )
    return dsn


async def main() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        result = await ensure_polaris_internal_gateway(conn)
    finally:
        await conn.close()
    logger.info("polaris-internal gateway ensured: %s", result)


if __name__ == "__main__":
    asyncio.run(main())
