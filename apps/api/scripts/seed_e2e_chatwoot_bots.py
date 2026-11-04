"""Seed a single platform-level 'Polaris Bot' across all Chatwoot accounts
and sync the access token back to every tenant's chatwoot_bot_token secret.

Run after seed_e2e_multitenant.py:

    uv run python apps/api/scripts/seed_e2e_chatwoot_bots.py

What it does:
  1. Uses `docker exec chatwoot-rails-1 bundle exec rails runner -` to:
       - Delete all existing agent bots.
       - Create ONE platform-level AgentBot (no account_id) named 'Polaris Bot'.
         Platform bots authenticate across every account — the single token
         is used for all API/v1/accounts/:id/* calls (per client.py ADR-051 /
         Q-PHASE7-01: "SINGLE platform-wide token").
       - Attach the bot to every inbox via AgentBotInbox so Chatwoot routes
         inbound webhooks through it.
       - Print TOKEN:<access_token>
  2. Writes the same token into Polaris tenant_secrets (kind='chatwoot_bot_token')
     for every tenant — forward-compat in case the client grows per-tenant
     resolution later.
  3. Prints CHATWOOT_BOT_ACCESS_TOKEN=<token> to paste into .env.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

sys.path.insert(0, str(Path(__file__).parents[2]))

from polaris_api.services.secrets import encrypt  # noqa: E402

logger = logging.getLogger(__name__)

# All known tenants — same UUIDs seeded by seed_e2e_multitenant.py.
ALL_TENANTS: list[UUID] = [
    UUID("00000000-0000-0000-0000-000000000001"),  # polaris-owner (account 1)
    UUID("00000000-0000-0000-0000-00000000000a"),  # tenant-a     (account 2)
    UUID("00000000-0000-0000-0000-00000000000b"),  # tenant-b     (account 3)
    UUID("00000000-0000-0000-0000-00000000000c"),  # tenant-c     (account 4)
    UUID("00000000-0000-0000-0000-00000000000d"),  # tenant-d     (account 5)
]

# Platform-level bot: no account_id → token authenticates against ANY
# account's /api/v1/accounts/:id/* endpoints, matching client.py design.
_RAILS_SCRIPT = """
AgentBot.destroy_all
bot = AgentBot.create!(name: 'Polaris Bot')
token = bot.access_token.token
Inbox.find_each { |inbox| AgentBotInbox.find_or_create_by!(inbox: inbox, agent_bot: bot) }
puts "TOKEN:#{token}"
""".strip()


def _service_dsn() -> str:
    dsn = os.environ.get("POLARIS_DB_URL_SERVICE") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("Set POLARIS_DB_URL_SERVICE in .env before running.")
    return dsn


def _run_rails_script() -> str:
    """Run the Rails script via docker exec; return the platform bot token."""
    cmd = [
        "docker", "exec", "-i", "chatwoot-rails-1",
        "bundle", "exec", "rails", "runner", "-",
    ]

    logger.info("running rails script in chatwoot-rails-1...")
    result = subprocess.run(
        cmd,
        input=_RAILS_SCRIPT,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise SystemExit(
            f"rails runner failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    for line in result.stdout.splitlines():
        if line.startswith("TOKEN:"):
            token = line.split(":", 1)[1].strip()
            if token:
                logger.info("platform bot token obtained")
                return token

    raise SystemExit(
        f"No TOKEN: line in rails output. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


async def _update_tenant_secret(
    conn: asyncpg.Connection, tenant_id: UUID, bot_token: str
) -> None:
    wire = encrypt(bot_token.encode(), tenant_id)
    version = wire[0]
    nonce = wire[1:25]
    ciphertext = wire[25:]
    await conn.execute(
        """
        INSERT INTO tenant_secrets
          (id, tenant_id, kind, ciphertext, nonce, algo, version, created_at)
        VALUES (gen_random_uuid(), $1, 'chatwoot_bot_token', $2, $3,
                'xchacha20poly1305', $4, NOW())
        ON CONFLICT (tenant_id, kind) DO UPDATE
          SET ciphertext = EXCLUDED.ciphertext,
              nonce      = EXCLUDED.nonce,
              version    = EXCLUDED.version
        """,
        tenant_id,
        ciphertext,
        nonce,
        version,
    )


async def _sync_token_to_all_tenants(platform_token: str) -> None:
    """Write the same platform token to every tenant's chatwoot_bot_token secret."""
    conn: asyncpg.Connection = await asyncpg.connect(_service_dsn())
    try:
        for tenant_id in ALL_TENANTS:
            await _update_tenant_secret(conn, tenant_id, platform_token)
            logger.info("updated chatwoot_bot_token for tenant=%s", tenant_id)
    finally:
        await conn.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    platform_token = _run_rails_script()
    asyncio.run(_sync_token_to_all_tenants(platform_token))

    print("\n--- Done ---")
    print("Add to .env:")
    print(f"  CHATWOOT_BOT_ACCESS_TOKEN={platform_token}")
    print("Platform 'Polaris Bot' created and installed on all inboxes.")
    print("chatwoot_bot_token secret updated for all 5 tenants in Polaris DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
