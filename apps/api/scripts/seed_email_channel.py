"""Seed an email channel for a tenant — email-channel M1 dev/e2e stand-in.

Idempotent. Stand-in for M3 self-serve connect: until the portal connect flow
lands, this wires the single row the email-assist path needs — a
``tenant_channels`` row with channel='email' bound to an existing Chatwoot
email inbox (``chatwoot_inbox_id``, required + UNIQUE, sales-led NOT NULL model).

Unlike the Telegram seed there is NO secret and NO token: Chatwoot owns the
IMAP/SMTP (or OAuth) credentials for the email inbox; Polaris only records the
inbox<->tenant binding so ``resolve_chatwoot()`` returns channel_kind='email'
and the email-assist consumer fires for inbound mail on this inbox.

It then attaches the shared 'Polaris Bot' agent-bot to the inbox (best-effort,
mirrors admin ``add_channel``) — without the bot on the inbox Chatwoot never
fires ``message_created`` to Polaris and no draft is ever produced. Re-running
reconciles the attach.

Prerequisite: the Chatwoot email inbox already exists (created in the Chatwoot
admin UI, M0) and you know its numeric inbox id.

Usage:
  uv run python -m apps.api.scripts.seed_email_channel \
      --tenant-id <uuid> --chatwoot-inbox-id <int>

Requires POLARIS_DATABASE_URL / SUPABASE_DB_URL (service DSN) in `.env`.
The agent-bot attach additionally needs CHATWOOT_BASE_URL + CHATWOOT_AGENT_API_TOKEN;
if those are unset the row is still seeded and a warning points at
``scripts/backfill_agent_bot_attach.py`` to reconcile later.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from polaris_api.config import get_settings  # noqa: E402 — after sys.path tweak
from polaris_api.services.chatwoot.agent_bot import (  # noqa: E402
    attach_inbox_agent_bot,
)
from polaris_api.services.chatwoot.client import get_chatwoot_client  # noqa: E402


def _dsn() -> str:
    settings = get_settings()
    dsn = settings.polaris_db_url_service or settings.polaris_db_url_anon
    if not dsn:
        raise SystemExit(
            "set POLARIS_DATABASE_URL / SUPABASE_DB_URL (service DSN) in .env"
        )
    return dsn


async def _attach_bot(*, account_id: int | None, inbox_id: int) -> None:
    """Best-effort: attach the shared 'Polaris Bot' to the email inbox.

    Mirrors admin ``add_channel`` (which attaches the bot unconditionally for
    every kind) so a seed-created email inbox isn't left webhook-silent. Without
    the bot on the inbox Chatwoot never fires ``message_created`` to Polaris and
    no draft is ever produced. NEVER raises — a Chatwoot outage / unset
    ``CHATWOOT_*`` env must not fail the DB seed; ops can re-run
    ``scripts/backfill_agent_bot_attach.py`` to reconcile.
    """
    if account_id is None:
        print(
            "WARN: tenant has no chatwoot_account_id — bot NOT attached; "
            "set it then run scripts/backfill_agent_bot_attach.py"
        )
        return
    try:
        # get_chatwoot_client() reads CHATWOOT_* env and may raise
        # ChatwootConfigError on construction (outside attach_inbox_agent_bot's
        # own guard), so the whole block is wrapped.
        attached = await attach_inbox_agent_bot(
            get_chatwoot_client(), account_id=account_id, inbox_id=inbox_id
        )
        if attached:
            print(f"agent-bot attached: account={account_id} inbox={inbox_id}")
        else:
            print(
                "WARN: agent-bot NOT attached (see logs) — "
                "run scripts/backfill_agent_bot_attach.py to reconcile"
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; never fail the seed
        print(
            f"WARN: agent-bot attach errored ({type(exc).__name__}: {exc}) — "
            "run scripts/backfill_agent_bot_attach.py to reconcile"
        )


async def _seed(*, tenant_id: UUID, chatwoot_inbox_id: int) -> None:
    conn = await asyncpg.connect(_dsn())
    account_id: int | None = None
    try:
        async with conn.transaction():
            # Scope RLS to the tenant for the channel write.
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_id)
            )

            existing = await conn.fetchrow(
                """
                SELECT id
                  FROM tenant_channels
                 WHERE tenant_id = $1 AND channel = 'email'
                   AND chatwoot_inbox_id = $2
                """,
                tenant_id,
                chatwoot_inbox_id,
            )
            if existing is not None:
                print(f"email channel already seeded: channel_id={existing['id']}")
            else:
                channel_id = await conn.fetchval(
                    """
                    INSERT INTO tenant_channels (
                      tenant_id, channel, chatwoot_inbox_id, status
                    )
                    VALUES ($1, 'email', $2, 'active')
                    RETURNING id
                    """,
                    tenant_id,
                    chatwoot_inbox_id,
                )
                print(f"seeded email channel: channel_id={channel_id}")

            # The owning Chatwoot account id is needed for the bot attach below.
            account_id = await conn.fetchval(
                "SELECT chatwoot_account_id FROM tenants WHERE id = $1", tenant_id
            )
    finally:
        await conn.close()

    # Post-commit, best-effort — reconciles the bot attach on every run
    # (idempotent), so re-running after a Chatwoot outage self-heals.
    await _attach_bot(account_id=account_id, inbox_id=chatwoot_inbox_id)


def main() -> None:
    parser = argparse.ArgumentParser(prog="seed_email_channel")
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument(
        "--chatwoot-inbox-id",
        required=True,
        type=int,
        help="UNIQUE id of the Chatwoot email inbox already created for this tenant",
    )
    args = parser.parse_args()

    asyncio.run(_seed(tenant_id=args.tenant_id, chatwoot_inbox_id=args.chatwoot_inbox_id))


if __name__ == "__main__":
    main()
