"""Seed a platform_admins row for E2E testing.

Inserts (or re-activates) a row in platform_admins for the E2E test email.
Safe to run repeatedly — ON CONFLICT DO UPDATE just re-activates the row if
it was previously deactivated.

Run before executing the admin Playwright E2E suite:

    uv run python apps/api/scripts/seed_e2e_admin.py

Required env vars (auto-loaded from repo-root .env):
    DATABASE_URL    — asyncpg-compatible Supabase connection string

Optional overrides:
    E2E_ADMIN_EMAIL   defaults to e2e-admin@polaris-local.dev
    E2E_ADMIN_NAME    display_name, defaults to "E2E Admin"
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

import asyncpg  # noqa: E402  (imported after dotenv so env is populated)

E2E_ADMIN_EMAIL: str = os.environ.get(
    "E2E_ADMIN_EMAIL", "e2e-admin@polaris-local.dev"
)
E2E_ADMIN_NAME: str = os.environ.get("E2E_ADMIN_NAME", "E2E Admin")
# Accept DATABASE_URL or the project-specific service-role URL.
DATABASE_URL: str | None = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POLARIS_DB_URL_SERVICE")
    or os.environ.get("SUPABASE_DB_URL")
)


async def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)

    email = E2E_ADMIN_EMAIL.lower().strip()

    conn = await asyncpg.connect(DATABASE_URL, ssl="require")
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO platform_admins (email, display_name, role, active)
            VALUES ($1, $2, 'platform_admin', TRUE)
            ON CONFLICT (email) DO UPDATE
              SET active       = TRUE,
                  display_name = EXCLUDED.display_name,
                  updated_at   = NOW()
            RETURNING id, email, display_name, active
            """,
            email,
            E2E_ADMIN_NAME,
        )
        print(
            f"OK  platform_admins row seeded\n"
            f"    id           = {row['id']}\n"
            f"    email        = {row['email']}\n"
            f"    display_name = {row['display_name']}\n"
            f"    active       = {row['active']}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
