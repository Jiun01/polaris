"""Seed portal E2E tenants + tenant_admin users for the /bookings live test.

Mirrors `seed_e2e_admin.py` shape. Idempotent — safe to re-run.

Creates (or re-activates):

  1. Tenant A — re-uses the existing `rasa-sayang-cafe` tenant + its admin
     `owner@rasasayang.my` (already provisioned by `seed_demo_*` scripts).
     This script just verifies the row is present; nothing is written.

  2. Tenant B — fresh tenant `e2e-bookings-b` (`E2E Bookings Tenant B`) +
     tenant_quotas row (plan_tier: starter) + a portal admin
     `e2e-portal-b@polaris-local.dev` linked via tenant_users.

Both admins log in via portal magic-link (no password). The bookings
multi-tenancy test then needs two browser contexts — one per admin —
hitting `/api/tenant/bookings/config` and verifying isolation.

Run before the bookings live test:

    uv run python apps/api/scripts/seed_e2e_portal.py

Required env vars (auto-loaded from repo-root .env):
    POLARIS_DB_URL_SERVICE  (or DATABASE_URL / SUPABASE_DB_URL fallback)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

import asyncpg  # noqa: E402

TENANT_A_SLUG = "rasa-sayang-cafe"
TENANT_A_ADMIN_EMAIL = "owner@rasasayang.my"

TENANT_B_SLUG = "e2e-bookings-b"
TENANT_B_DISPLAY = "E2E Bookings Tenant B"
TENANT_B_ADMIN_EMAIL = os.environ.get(
    "E2E_PORTAL_B_EMAIL", "e2e-portal-b@polaris-local.dev"
)

DATABASE_URL: str | None = (
    os.environ.get("POLARIS_DB_URL_SERVICE")
    or os.environ.get("DATABASE_URL")
    or os.environ.get("SUPABASE_DB_URL")
)


async def main() -> None:
    if not DATABASE_URL:
        print("ERROR: POLARIS_DB_URL_SERVICE / DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL, ssl="require")
    try:
        # --- Tenant A: verify existing seed -----------------------------------
        row_a = await conn.fetchrow(
            "SELECT id, slug, status FROM tenants WHERE slug = $1",
            TENANT_A_SLUG,
        )
        if row_a is None:
            print(
                f"ERROR: Tenant A '{TENANT_A_SLUG}' missing. Run the demo seeds first.",
                file=sys.stderr,
            )
            sys.exit(2)
        user_row_a = await conn.fetchrow(
            "SELECT * FROM public.get_portal_user_by_email($1)",
            TENANT_A_ADMIN_EMAIL.lower(),
        )
        if user_row_a is None:
            print(
                f"ERROR: Tenant A admin '{TENANT_A_ADMIN_EMAIL}' not found in auth.users.",
                file=sys.stderr,
            )
            sys.exit(3)
        print(f"OK  Tenant A '{TENANT_A_SLUG}' id={row_a['id']}")
        print(f"OK  Tenant A admin '{TENANT_A_ADMIN_EMAIL}' user_id={user_row_a['id']}")

        # --- Tenant B: create or re-activate ---------------------------------
        tenant_b_id: UUID = await conn.fetchval(
            """
            INSERT INTO tenants (slug, display_name, status, plan_tier, timezone)
            VALUES ($1, $2, 'active', 'starter', 'Asia/Kuala_Lumpur')
            ON CONFLICT (slug) DO UPDATE
              SET status = 'active',
                  display_name = EXCLUDED.display_name,
                  suspended_reason = NULL,
                  updated_at = NOW()
            RETURNING id
            """,
            TENANT_B_SLUG,
            TENANT_B_DISPLAY,
        )
        print(f"OK  Tenant B '{TENANT_B_SLUG}' id={tenant_b_id}")

        # tenant_quotas — many NOT NULL columns without defaults; mirror the
        # starter-tier row shape provisioned by the live admin flow. The exact
        # numeric ceilings don't matter for the bookings test, just that the
        # row exists so JWT mint can read plan_tier without LEFT-JOIN nulls.
        await conn.execute(
            """
            INSERT INTO tenant_quotas (
              tenant_id, plan_tier,
              bot_replies, bot_replies_period_cap,
              vision_calls_per_month,
              conversation_retention_days, audit_log_retention_days,
              ingestion_requests_retention_days, retention_message_days,
              allowed_channel_kinds, outbound_broadcast_enabled,
              multi_branch_enabled, knowledge_chunks_total_mb,
              knowledge_chunks_total_mb_cap, rate_limit_per_minute,
              monthly_llm_tokens_in, monthly_llm_tokens_out,
              monthly_ingestion_pages, monthly_broadcast_recipients,
              monthly_bookings, agent_seats_included, agent_seats_extra
            ) VALUES (
              $1, 'starter',
              5000, 5000,
              0,
              90, 0,
              90, 90,
              ARRAY['whatsapp']::text[], FALSE,
              FALSE, 100,
              100, 60,
              500000, 500000,
              500, 5000,
              500, 1, 0
            )
            ON CONFLICT (tenant_id) DO UPDATE
              SET plan_tier = EXCLUDED.plan_tier,
                  updated_at = NOW()
            """,
            tenant_b_id,
        )
        print("OK  Tenant B quotas row (plan_tier=starter)")

        # --- Tenant B admin via SECURITY DEFINER helper ----------------------
        user_b_id: UUID = await conn.fetchval(
            "SELECT public.upsert_auth_user_by_email($1)",
            TENANT_B_ADMIN_EMAIL.lower(),
        )
        await conn.execute(
            """
            INSERT INTO tenant_users (tenant_id, user_id, role)
            VALUES ($1, $2, 'admin')
            ON CONFLICT (tenant_id, user_id) DO UPDATE
              SET role = EXCLUDED.role
            """,
            tenant_b_id,
            user_b_id,
        )
        print(
            f"OK  Tenant B admin '{TENANT_B_ADMIN_EMAIL}' user_id={user_b_id} role=admin"
        )

        # --- Summary --------------------------------------------------------
        print()
        print("Seed complete. Magic-link sign-in via MailHog:")
        print(f"  Tenant A: {TENANT_A_ADMIN_EMAIL}  (slug={TENANT_A_SLUG})")
        print(f"  Tenant B: {TENANT_B_ADMIN_EMAIL}  (slug={TENANT_B_SLUG})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
