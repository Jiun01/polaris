"""
One-shot setup: create e2e-tenant-a in Polaris DB, wired to CW Account 1.
Run: uv run python scripts/setup_e2e_cw_tenant.py
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Load .env
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"')
        os.environ.setdefault(k, v)

import asyncpg

sys.path.insert(0, str(Path("apps/api")))
from polaris_api.services.secrets import encrypt

DB_URL = os.environ["SUPABASE_DB_URL"]

E2E_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000e2e")
E2E_SLUG = "e2e-tenant-a"
CW_ACCOUNT_ID = 1
CW_INBOX_ID = 1
BOT_TOKEN = "gGbpYh698j2dfiLu5ZkzfKV5"
HMAC_SECRET = "e2e-test-hmac-secret-polaris-cw"


async def main() -> None:
    conn = await asyncpg.connect(DB_URL)

    # Upsert tenant
    await conn.execute(
        """
        INSERT INTO tenants (id, slug, status, plan_tier, chatwoot_account_id,
            created_at, updated_at)
        VALUES ($1, $2, 'active', 'starter', $3, now(), now())
        ON CONFLICT (id) DO UPDATE
          SET chatwoot_account_id = EXCLUDED.chatwoot_account_id,
              updated_at = now()
        """,
        E2E_TENANT_ID, E2E_SLUG, CW_ACCOUNT_ID,
    )
    print(f"Tenant upserted: {E2E_SLUG} (chatwoot_account_id={CW_ACCOUNT_ID})")

    # Upsert tenant_channels — delete + insert since no unique constraint on (tenant_id, channel)
    await conn.execute(
        "DELETE FROM tenant_channels WHERE tenant_id = $1 AND channel = 'whatsapp'",
        E2E_TENANT_ID,
    )
    await conn.execute(
        """
        INSERT INTO tenant_channels (id, tenant_id, channel, chatwoot_inbox_id,
            status, created_at, messenger_icebreakers, messenger_persistent_menu)
        VALUES ($1, $2, 'whatsapp', $3, 'active', now(), '[]', '[]')
        """,
        uuid.uuid4(), E2E_TENANT_ID, CW_INBOX_ID,
    )
    print(f"tenant_channels upserted: inbox_id={CW_INBOX_ID}")

    # Store bot token in tenant_secrets
    async def upsert_secret(kind: str, plaintext: str) -> None:
        wire = encrypt(plaintext.encode(), E2E_TENANT_ID)
        # version byte is first byte, rest is nonce(24) + ciphertext + tag(16)
        version = wire[0]
        rest = wire[1:]
        nonce = rest[:24]
        ciphertext = rest[24:]
        await conn.execute(
            """
            INSERT INTO tenant_secrets
              (id, tenant_id, kind, ciphertext, nonce, algo, version, created_at)
            VALUES ($1, $2, $3, $4, $5, 'xchacha20poly1305', $6, now())
            ON CONFLICT (tenant_id, kind) DO UPDATE
              SET ciphertext = EXCLUDED.ciphertext,
                  nonce      = EXCLUDED.nonce,
                  version    = EXCLUDED.version,
                  rotated_at = now()
            """,
            uuid.uuid4(), E2E_TENANT_ID, kind,
            ciphertext, nonce, version,
        )
        print(f"  Secret '{kind}' stored.")

    await upsert_secret("chatwoot_bot_token", BOT_TOKEN)
    await upsert_secret("chatwoot_webhook_hmac_secret", HMAC_SECRET)

    # Verify
    row = await conn.fetchrow(
        "SELECT slug, chatwoot_account_id FROM tenants WHERE id = $1", E2E_TENANT_ID
    )
    chans = await conn.fetch(
        "SELECT chatwoot_inbox_id FROM tenant_channels WHERE tenant_id = $1", E2E_TENANT_ID
    )
    secrets = await conn.fetch(
        "SELECT kind FROM tenant_secrets WHERE tenant_id = $1", E2E_TENANT_ID
    )
    print("\nVerification:")
    print(f"  tenant: {dict(row)}")
    print(f"  channels: {[dict(c) for c in chans]}")
    print(f"  secrets: {[dict(s) for s in secrets]}")
    print(f"\nHMAC_SECRET for CW webhook config: {HMAC_SECRET!r}")
    print("P-12 ✅  P-13 ✅  P-14 setup done")
    await conn.close()


asyncio.run(main())
