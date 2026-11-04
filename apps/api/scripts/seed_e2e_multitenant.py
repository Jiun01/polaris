"""Seed the 5-tenant E2E fixture in the Polaris DB.

Spec: ../../docs/Polaris V2/e2e/01-seed-and-teardown.md §6.

Creates / re-activates: Polaris owner (ultra), Tenant A (starter WA),
Tenant B (starter IG), Tenant C (pro full-channel), Tenant D (pro
multi-branch). Wires each tenant's quotas, channels, branches,
secrets, audience members, and the tenant_admin user (magic-link
sign-in via portal at :3000).

Companion scripts:
  - Chatwoot side: ``polaris-cw-fork/lib/tasks/polaris_seed.rake``
  - Easy!Appointments side: ``seed_e2e_ea_providers.py``

Run::

    uv run python apps/api/scripts/seed_e2e_multitenant.py [--reset]
                  [--teardown] [--tenant=<slug>] [--skip-knowledge]
                  [--skip-flows] [--verbose]

Required env vars (auto-loaded from repo-root .env):
    POLARIS_DB_URL_SERVICE  (or DATABASE_URL / SUPABASE_DB_URL)
    POLARIS_KMS_KEY         (for secret encryption; do NOT regenerate)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

# Add apps/api to sys.path so polaris_api imports resolve.
sys.path.insert(0, str(Path(__file__).parents[2]))

import asyncpg  # noqa: E402
from polaris_api.services.demo_sessions import (  # noqa: E402
    POLARIS_INTERNAL_TENANT_ID,
)
from polaris_api.services.secrets import encrypt  # noqa: E402

logger = logging.getLogger("seed_e2e_multitenant")


# ─────────────────────────────────────────────────────────────────────
# Fixture data — matches docs/Polaris V2/e2e/01-seed-and-teardown.md
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChannelSpec:
    channel: str  # 'whatsapp' | 'instagram' | 'messenger'
    chatwoot_inbox_id: int
    waba_id: str | None = None
    phone_number_id: str | None = None
    ig_business_account_id: str | None = None
    fb_page_id: str | None = None


@dataclass(frozen=True)
class BranchSpec:
    slug: str
    name: str
    timezone: str
    hours: dict  # weekday -> {open, close, closed}


@dataclass(frozen=True)
class AudienceSpec:
    channel: str
    handle: str  # phone (WA) or username (IG) or PSID (Messenger)
    name: str
    opted_in: bool
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TenantSpec:
    id: UUID
    slug: str
    display_name: str
    plan_tier: str  # 'starter' | 'pro' | 'ultra'
    chatwoot_account_id: int
    admin_email: str
    admin_display_name: str
    channels: tuple[ChannelSpec, ...]
    branches: tuple[BranchSpec, ...]
    secrets: dict[str, str]  # kind -> plaintext
    audience: tuple[AudienceSpec, ...]
    timezone: str = "Asia/Kuala_Lumpur"
    status: str = "active"  # 'active' | 'suspended' | 'deleted'
    suspended_reason: str | None = None  # 'quota_exhausted' | 'payment_overdue'


# Plan-tier numeric defaults (per proration.py).
_PLAN_DEFAULTS: dict[str, dict] = {
    "starter": dict(
        bot_replies_period_cap=5_000,
        knowledge_chunks_total_mb=50,
        knowledge_chunks_total_mb_cap=50,
        outbound_broadcast_enabled=False,
        multi_branch_enabled=False,
        agent_seats_included=1,
    ),
    "pro": dict(
        bot_replies_period_cap=25_000,
        knowledge_chunks_total_mb=200,
        knowledge_chunks_total_mb_cap=200,
        outbound_broadcast_enabled=True,  # pro+
        multi_branch_enabled=True,
        agent_seats_included=3,
    ),
    "ultra": dict(
        bot_replies_period_cap=60_000,
        knowledge_chunks_total_mb=1_000,
        knowledge_chunks_total_mb_cap=1_000,
        outbound_broadcast_enabled=True,
        multi_branch_enabled=True,
        agent_seats_included=5,
    ),
}

_BUSINESS_HOURS_DEFAULT = {
    "mon": {"open": "09:00", "close": "18:00", "closed": False},
    "tue": {"open": "09:00", "close": "18:00", "closed": False},
    "wed": {"open": "09:00", "close": "18:00", "closed": False},
    "thu": {"open": "09:00", "close": "18:00", "closed": False},
    "fri": {"open": "09:00", "close": "18:00", "closed": False},
    "sat": {"open": "10:00", "close": "14:00", "closed": False},
    "sun": {"open": "00:00", "close": "00:00", "closed": True},
}


def _build_fixture() -> list[TenantSpec]:
    """Return the five-tenant fixture, deterministic UUIDs."""
    polaris = TenantSpec(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        slug="polaris-owner",
        display_name="Polaris HQ",
        plan_tier="ultra",
        chatwoot_account_id=1,
        admin_email="owner@getpolaris.my",
        admin_display_name="Polaris Owner",
        channels=(
            ChannelSpec(
                channel="whatsapp",
                chatwoot_inbox_id=2,
                waba_id="1904910020385746",
                phone_number_id="1043463478844443",
            ),
            ChannelSpec(channel="messenger", chatwoot_inbox_id=3,
                        ig_business_account_id="e2e_ig_polaris_001"),
            ChannelSpec(channel="messenger", chatwoot_inbox_id=4,
                        fb_page_id="e2e_fb_polaris_001"),
        ),
        branches=(
            BranchSpec(
                slug="polaris-hq",
                name="Polaris HQ (Kuala Lumpur)",
                timezone="Asia/Kuala_Lumpur",
                hours=_BUSINESS_HOURS_DEFAULT,
            ),
        ),
        secrets={
            "meta_wa_token": "e2e_polaris_wa",
            "meta_ig_token": "e2e_polaris_ig",
            "meta_fb_token": "e2e_polaris_fb",
            "chatwoot_bot_token": "Yi4oCyCKeiTJk6fH9SMqq3Cq",
            "chatwoot_webhook_hmac_secret": "e2e_polaris_hmac",
            "ea_api_token": "e2e_polaris_ea",
        },
        audience=(
            AudienceSpec(channel="whatsapp", handle="+60123450001",
                         name="Demo Prospect One", opted_in=True),
            AudienceSpec(channel="whatsapp", handle="+60123450002",
                         name="Demo Prospect Two", opted_in=True),
            AudienceSpec(channel="whatsapp", handle="+60123450003",
                         name="Subscription Lead", opted_in=True,
                         metadata={"intent": "subscription"}),
        ),
    )

    tenant_a = TenantSpec(
        id=UUID("00000000-0000-0000-0000-00000000000a"),
        slug="tenant-a",
        display_name="Tenant A",
        plan_tier="starter",
        chatwoot_account_id=2,
        admin_email="admin-a@e2e.polaris-local.dev",
        admin_display_name="Tenant A Admin",
        channels=(ChannelSpec(channel="whatsapp", chatwoot_inbox_id=5,
                              waba_id="e2e_waba_a_001"),),
        branches=(),
        secrets={
            "meta_wa_token": "e2e_a_wa",
            "chatwoot_bot_token": "1yNtJoxq63Wq9kaUcUzafnL4",
            "chatwoot_webhook_hmac_secret": "e2e_a_hmac",
        },
        audience=(
            AudienceSpec(channel="whatsapp", handle="+60123450010",
                         name="A Customer 1", opted_in=True),
            AudienceSpec(channel="whatsapp", handle="+60123450011",
                         name="A Customer 2", opted_in=False),
        ),
    )

    tenant_b = TenantSpec(
        id=UUID("00000000-0000-0000-0000-00000000000b"),
        slug="tenant-b",
        display_name="Tenant B",
        plan_tier="starter",
        chatwoot_account_id=3,
        admin_email="admin-b@e2e.polaris-local.dev",
        admin_display_name="Tenant B Admin",
        channels=(ChannelSpec(channel="messenger", chatwoot_inbox_id=6,
                              ig_business_account_id="e2e_ig_b_001"),),
        branches=(),
        secrets={
            "meta_ig_token": "e2e_b_ig",
            "chatwoot_bot_token": "mV2Bkkr78BBZzhvMkPvMSAeS",
            "chatwoot_webhook_hmac_secret": "e2e_b_hmac",
        },
        audience=(
            AudienceSpec(channel="messenger", handle="@b_customer_1",
                         name="B Customer 1", opted_in=True),
            AudienceSpec(channel="messenger", handle="@b_customer_2",
                         name="B Customer 2", opted_in=True),
        ),
    )

    tenant_c = TenantSpec(
        id=UUID("00000000-0000-0000-0000-00000000000c"),
        slug="tenant-c",
        display_name="Tenant C",
        plan_tier="pro",
        chatwoot_account_id=4,
        admin_email="admin-c@e2e.polaris-local.dev",
        admin_display_name="Tenant C Admin",
        channels=(
            ChannelSpec(channel="whatsapp", chatwoot_inbox_id=7,
                        waba_id="e2e_waba_c_001"),
            ChannelSpec(channel="messenger", chatwoot_inbox_id=8,
                        ig_business_account_id="e2e_ig_c_001"),
            ChannelSpec(channel="messenger", chatwoot_inbox_id=9,
                        fb_page_id="e2e_fb_c_001"),
        ),
        branches=(
            BranchSpec(
                slug="tenant-c-hq",
                name="Tenant C HQ",
                timezone="Asia/Kuala_Lumpur",
                hours={
                    "mon": {"open": "10:00", "close": "20:00", "closed": False},
                    "tue": {"open": "10:00", "close": "20:00", "closed": False},
                    "wed": {"open": "10:00", "close": "20:00", "closed": False},
                    "thu": {"open": "10:00", "close": "20:00", "closed": False},
                    "fri": {"open": "10:00", "close": "20:00", "closed": False},
                    "sat": {"open": "10:00", "close": "20:00", "closed": False},
                    "sun": {"open": "00:00", "close": "00:00", "closed": True},
                },
            ),
        ),
        secrets={
            "meta_wa_token": "e2e_c_wa",
            "meta_ig_token": "e2e_c_ig",
            "meta_fb_token": "e2e_c_fb",
            "chatwoot_bot_token": "mKGw13RMiekd9ATxVXcnZFRD",
            "chatwoot_webhook_hmac_secret": "e2e_c_hmac",
            "ea_api_token": "e2e_c_ea",
        },
        audience=(
            AudienceSpec(channel="whatsapp", handle="+60123450030",
                         name="C Customer 1", opted_in=True),
            AudienceSpec(channel="messenger", handle="@c_customer_2",
                         name="C Customer 2", opted_in=False),
            AudienceSpec(channel="messenger", handle="c_psid_3",
                         name="C Customer 3", opted_in=True),
        ),
    )

    tenant_d = TenantSpec(
        id=UUID("00000000-0000-0000-0000-00000000000d"),
        slug="tenant-d",
        display_name="Tenant D",
        plan_tier="pro",
        chatwoot_account_id=5,
        admin_email="admin-d@e2e.polaris-local.dev",
        admin_display_name="Tenant D Admin",
        channels=(
            ChannelSpec(channel="whatsapp", chatwoot_inbox_id=10,
                        waba_id="e2e_waba_d_001"),
            ChannelSpec(channel="messenger", chatwoot_inbox_id=11,
                        ig_business_account_id="e2e_ig_d_001"),
        ),
        branches=(
            BranchSpec(
                slug="tenant-d-hq",
                name="Tenant D HQ",
                timezone="Asia/Kuala_Lumpur",
                hours={
                    "mon": {"open": "09:00", "close": "17:00", "closed": False},
                    "tue": {"open": "09:00", "close": "17:00", "closed": False},
                    "wed": {"open": "09:00", "close": "17:00", "closed": False},
                    "thu": {"open": "09:00", "close": "17:00", "closed": False},
                    "fri": {"open": "09:00", "close": "17:00", "closed": False},
                    "sat": {"open": "00:00", "close": "00:00", "closed": True},
                    "sun": {"open": "00:00", "close": "00:00", "closed": True},
                },
            ),
            BranchSpec(
                slug="tenant-d-branch-2",
                name="Tenant D Branch 2",
                timezone="Asia/Kuala_Lumpur",
                hours={
                    "mon": {"open": "00:00", "close": "00:00", "closed": True},
                    "tue": {"open": "00:00", "close": "00:00", "closed": True},
                    "wed": {"open": "12:00", "close": "20:00", "closed": False},
                    "thu": {"open": "12:00", "close": "20:00", "closed": False},
                    "fri": {"open": "12:00", "close": "20:00", "closed": False},
                    "sat": {"open": "12:00", "close": "20:00", "closed": False},
                    "sun": {"open": "12:00", "close": "20:00", "closed": False},
                },
            ),
        ),
        secrets={
            "meta_wa_token": "e2e_d_wa",
            "meta_ig_token": "e2e_d_ig",
            "chatwoot_bot_token": "bBnLqRJLaVhdYMkUrrg4E13J",
            "chatwoot_webhook_hmac_secret": "e2e_d_hmac",
            "ea_api_token": "e2e_d_ea",
        },
        audience=(
            AudienceSpec(channel="whatsapp", handle="+60123450040",
                         name="D Customer 1", opted_in=True),
            AudienceSpec(channel="messenger", handle="@d_customer_2",
                         name="D Customer 2", opted_in=True),
        ),
    )

    # Tenant E — payment_overdue fixture (route-takeover to /pay). Starter, minimal.
    tenant_e = TenantSpec(
        id=UUID("00000000-0000-0000-0000-00000000000e"),
        slug="tenant-e-overdue",
        display_name="Tenant E (Overdue)",
        plan_tier="starter",
        chatwoot_account_id=6,
        admin_email="admin-e@e2e.polaris-local.dev",
        admin_display_name="Tenant E Admin",
        status="suspended",
        suspended_reason="payment_overdue",
        channels=(ChannelSpec(channel="whatsapp", chatwoot_inbox_id=60,
                              waba_id="e2e_waba_e_001"),),
        branches=(),
        secrets={
            "meta_wa_token": "e2e_e_wa",
            "chatwoot_bot_token": "1yNtJoxq63Wq9kaUcUzafnL4",
            "chatwoot_webhook_hmac_secret": "e2e_e_hmac",
        },
        audience=(
            AudienceSpec(channel="whatsapp", handle="+60123450060",
                         name="E Customer 1", opted_in=True),
        ),
    )

    return [polaris, tenant_a, tenant_b, tenant_c, tenant_d, tenant_e]


# ─────────────────────────────────────────────────────────────────────
# Seeding operations
# ─────────────────────────────────────────────────────────────────────


def _gateway_clobber_allowed() -> bool:
    """True when the operator explicitly opted in to touching the gateway row."""
    return os.environ.get("POLARIS_ALLOW_GATEWAY_CLOBBER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


async def _guard_gateway(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    """Refuse to mutate the polaris-internal demo gateway.

    This fixture's ``polaris-owner`` tenant shares the gateway's fixed UUID
    (``...0001``) — a historical collision. On an E2E/local DB the ``...0001``
    row is absent or already ``polaris-owner`` and this is a no-op. But if the
    row is the real ``polaris-internal`` gateway (i.e. this DSN points at prod),
    running --reset/--teardown here would delete or overwrite it — the exact bug
    this guard stops. Override only with ``POLARIS_ALLOW_GATEWAY_CLOBBER=1``.
    """
    if tenant_id != POLARIS_INTERNAL_TENANT_ID:
        return
    slug = await conn.fetchval("SELECT slug FROM tenants WHERE id = $1", tenant_id)
    if slug == "polaris-internal" and not _gateway_clobber_allowed():
        raise SystemExit(
            "REFUSING to seed/teardown: id 00000000-0000-0000-0000-000000000001 in this "
            "database is the real 'polaris-internal' demo gateway, not the E2E "
            "'polaris-owner' fixture. This script shares the gateway's UUID (known "
            "collision), so running it here would destroy the gateway. If you are certain "
            "this is a throwaway E2E/local DB, set POLARIS_ALLOW_GATEWAY_CLOBBER=1 to override."
        )


async def _delete_tenant(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    """Delete all rows belonging to one tenant.

    Uses migration 064's SECURITY DEFINER function for the noncascade
    set (audit_logs, audience_members, demo_sessions, tenant_invoices),
    then does the FK-ordered deletes for everything else.
    """
    await _guard_gateway(conn, tenant_id)
    exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM tenants WHERE id = $1)", tenant_id
    )
    if not exists:
        logger.info("  tenant %s not in DB, skipping delete", tenant_id)
        return
    # broadcast_recipients FK-reference audience_members.id, which the 064
    # noncascade function deletes — so clear the broadcast subtree first to
    # avoid a ForeignKeyViolationError when a prior E2E run left recipients.
    for pre_table in ("broadcast_recipients", "broadcast_messages", "broadcasts"):
        try:
            await conn.execute(
                f"DELETE FROM {pre_table} WHERE tenant_id = $1", tenant_id  # noqa: S608
            )
        except asyncpg.UndefinedTableError:
            logger.warning(f"  Skipped pre-delete from {pre_table} (table missing)")
    # 064's function handles the awkward set first.
    await conn.execute(
        "SELECT public.delete_e2e_tenant_noncascade_rows($1)", tenant_id
    )
    # Manual FK-ordered deletes for the rest.
    ordered = [
        ("webhook_events", "tenant_id"),
        ("messages", "tenant_id"),
        ("conversations", "tenant_id"),
        ("broadcast_recipients", "tenant_id"),
        ("broadcast_messages", "tenant_id"),
        ("broadcasts", "tenant_id"),
        ("bookings", "tenant_id"),
        ("knowledge_chunks", "tenant_id"),
        ("knowledge_sources", "tenant_id"),
        ("ingestion_runs", "tenant_id"),
        ("ingestion_requests", "tenant_id"),
        ("flow_runs", "tenant_id"),  # FK to flow_versions has no cascade
        ("flow_definitions", "tenant_id"),  # cascades into flow_versions
        ("flow_versions", "tenant_id"),
        ("flows", "tenant_id"),
        ("audience_segments", "tenant_id"),
        ("tenant_secrets", "tenant_id"),
        ("tenant_quotas", "tenant_id"),
        ("tenant_billing", "tenant_id"),
        ("tenant_users", "tenant_id"),
        ("tenant_channels", "tenant_id"),
        ("tenant_branches", "tenant_id"),
        ("tenants", "id"),
    ]
    for table, col in ordered:
        try:
            await conn.execute(
                f"DELETE FROM {table} WHERE {col} = $1", tenant_id  # noqa: S608
            )
        except asyncpg.UndefinedTableError:
            # Some tables (e.g. broadcast_recipients) may not exist
            # depending on which migrations are applied. Skip with a
            # warning rather than abort.
            logger.warning(f"  Skipped delete from {table} (table missing)")


async def _upsert_tenant(conn: asyncpg.Connection, t: TenantSpec) -> None:
    """INSERT … ON CONFLICT (id) DO UPDATE on the tenants row."""
    await _guard_gateway(conn, t.id)
    await conn.execute(
        """
        INSERT INTO tenants (
          id, slug, status, suspended_reason, plan_tier, timezone, chatwoot_account_id,
          display_name, business_hours, settings, created_at, updated_at
        )
        VALUES (
          $1, $2, $8::tenant_status, $9, $3::tenant_plan_tier, $4, $5,
          $10, $6::jsonb, $7::jsonb, NOW(), NOW()
        )
        ON CONFLICT (id) DO UPDATE
          SET slug = EXCLUDED.slug,
              status = EXCLUDED.status,
              plan_tier = EXCLUDED.plan_tier,
              timezone = EXCLUDED.timezone,
              chatwoot_account_id = EXCLUDED.chatwoot_account_id,
              suspended_reason = EXCLUDED.suspended_reason,
              display_name = EXCLUDED.display_name,
              updated_at = NOW()
        """,
        t.id, t.slug, t.plan_tier, t.timezone, t.chatwoot_account_id,
        json.dumps(_BUSINESS_HOURS_DEFAULT),
        json.dumps({"booking_config": {
            "cancellation_lead_hours": 24,
            "reschedule_window_hours": 24,
            "confirmation_message": "",
            "allow_self_serve_cancellation": True,
        }}),
        t.status,
        t.suspended_reason,
        t.display_name,
    )


async def _upsert_quotas(conn: asyncpg.Connection, t: TenantSpec) -> None:
    """INSERT … ON CONFLICT (tenant_id) DO UPDATE on tenant_quotas."""
    defaults = _PLAN_DEFAULTS[t.plan_tier]
    allowed_kinds = [c.channel for c in t.channels]
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
          $1, $2,
          $3, $3,
          0,
          90, 0,
          90, 90,
          $4::text[], $5,
          $6, $7,
          $7, 60,
          500000, 500000,
          500, 5000,
          500, $8, 0
        )
        ON CONFLICT (tenant_id) DO UPDATE
          SET plan_tier = EXCLUDED.plan_tier,
              bot_replies_period_cap = EXCLUDED.bot_replies_period_cap,
              allowed_channel_kinds = EXCLUDED.allowed_channel_kinds,
              outbound_broadcast_enabled = EXCLUDED.outbound_broadcast_enabled,
              multi_branch_enabled = EXCLUDED.multi_branch_enabled,
              knowledge_chunks_total_mb = EXCLUDED.knowledge_chunks_total_mb,
              knowledge_chunks_total_mb_cap = EXCLUDED.knowledge_chunks_total_mb_cap,
              updated_at = NOW()
        """,
        t.id, t.plan_tier,
        defaults["bot_replies_period_cap"],
        allowed_kinds,
        defaults["outbound_broadcast_enabled"],
        defaults["multi_branch_enabled"],
        defaults["knowledge_chunks_total_mb"],
        defaults["agent_seats_included"],
    )


async def _upsert_channels(conn: asyncpg.Connection, t: TenantSpec) -> None:
    # Delete-then-insert because no unique constraint on (tenant_id, channel).
    await conn.execute(
        "DELETE FROM tenant_channels WHERE tenant_id = $1", t.id
    )
    for c in t.channels:
        await conn.execute(
            """
            INSERT INTO tenant_channels
              (id, tenant_id, channel, chatwoot_inbox_id, waba_id, phone_number_id,
               ig_business_account_id, fb_page_id,
               status, created_at, messenger_icebreakers, messenger_persistent_menu)
            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, 'active', NOW(), '[]', '[]')
            """,
            t.id, c.channel, c.chatwoot_inbox_id, c.waba_id, c.phone_number_id,
            c.ig_business_account_id, c.fb_page_id,
        )


async def _upsert_branches(conn: asyncpg.Connection, t: TenantSpec) -> None:
    # Branches may not exist for starter tenants — skip if empty.
    if not t.branches:
        await conn.execute(
            "DELETE FROM tenant_branches WHERE tenant_id = $1", t.id
        )
        return
    # Delete then insert.
    await conn.execute(
        "DELETE FROM tenant_branches WHERE tenant_id = $1", t.id
    )
    for b in t.branches:
        await conn.execute(
            """
            INSERT INTO tenant_branches (id, tenant_id, slug, name, created_at)
            VALUES (gen_random_uuid(), $1, $2, $3, NOW())
            """,
            t.id, b.slug, b.name,
        )


async def _upsert_secrets(conn: asyncpg.Connection, t: TenantSpec) -> None:
    """Encrypted upsert per tenant_secrets schema."""
    for kind, plaintext in t.secrets.items():
        wire = encrypt(plaintext.encode(), t.id)
        version = wire[0]
        nonce = wire[1:25]
        ciphertext = wire[25:]
        await conn.execute(
            """
            INSERT INTO tenant_secrets
              (id, tenant_id, kind, ciphertext, nonce, algo, version, created_at)
            VALUES (gen_random_uuid(), $1, $2, $3, $4, 'xchacha20poly1305', $5, NOW())
            ON CONFLICT (tenant_id, kind) DO UPDATE
              SET ciphertext = EXCLUDED.ciphertext,
                  nonce = EXCLUDED.nonce,
                  version = EXCLUDED.version,
                  rotated_at = NOW()
            """,
            t.id, kind, ciphertext, nonce, version,
        )


async def _upsert_admin_user(conn: asyncpg.Connection, t: TenantSpec) -> None:
    """Create auth.users + tenant_users (admin role) via SECURITY DEFINER helpers."""
    user_id = await conn.fetchval(
        "SELECT public.upsert_auth_user_by_email($1)", t.admin_email.lower()
    )
    await conn.execute(
        """
        INSERT INTO tenant_users (tenant_id, user_id, role)
        VALUES ($1, $2, 'admin')
        ON CONFLICT (tenant_id, user_id) DO UPDATE
          SET role = EXCLUDED.role
        """,
        t.id, user_id,
    )


async def _upsert_audience(conn: asyncpg.Connection, t: TenantSpec) -> None:
    # Delete-then-insert; no natural key per (tenant, channel, handle).
    await conn.execute(
        "DELETE FROM audience_members WHERE tenant_id = $1", t.id
    )
    for a in t.audience:
        await conn.execute(
            """
            INSERT INTO audience_members
              (id, tenant_id, channel, channel_user_id, channel_user_name,
               opted_in_at, opt_in_source, metadata, created_at, updated_at)
            VALUES (gen_random_uuid(), $1, $2, $3, $4,
                    NOW(), 'api_sync', $5::jsonb, NOW(), NOW())
            """,
            t.id, a.channel, a.handle, a.name, json.dumps(a.metadata),
        )


# ─────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────


_EXPECTED_COUNTS: dict[str, dict[str, int]] = {
    "polaris-owner": dict(channels=3, branches=1, secrets=6, audience=3),
    "tenant-a":      dict(channels=1, branches=0, secrets=3, audience=2),
    "tenant-b":      dict(channels=1, branches=0, secrets=3, audience=2),
    "tenant-c":      dict(channels=3, branches=1, secrets=6, audience=3),
    "tenant-d":      dict(channels=2, branches=2, secrets=5, audience=2),
    "tenant-e-overdue": dict(channels=1, branches=0, secrets=3, audience=1),
}


async def _verify(conn: asyncpg.Connection, slugs: list[str]) -> bool:
    rows = await conn.fetch(
        """
        SELECT t.slug, t.plan_tier::text AS plan_tier,
               t.chatwoot_account_id AS cw_acct,
               (SELECT COUNT(*) FROM tenant_channels tc WHERE tc.tenant_id = t.id) AS channels,
               (SELECT COUNT(*) FROM tenant_branches tb WHERE tb.tenant_id = t.id) AS branches,
               (SELECT COUNT(*) FROM tenant_secrets ts WHERE ts.tenant_id = t.id) AS secrets,
               (SELECT COUNT(*) FROM audience_members am WHERE am.tenant_id = t.id) AS audience
        FROM tenants t
        WHERE t.slug = ANY($1::text[])
        ORDER BY t.slug
        """,
        slugs,
    )
    all_ok = True
    print("\nVerification — actual row counts:")
    print(f"  {'slug':<16} {'tier':<8} {'cw':<3} ch br se au")
    for row in rows:
        expected = _EXPECTED_COUNTS.get(row["slug"], {})
        ok = (
            row["channels"] == expected.get("channels")
            and row["branches"] == expected.get("branches")
            and row["secrets"] == expected.get("secrets")
            and row["audience"] == expected.get("audience")
        )
        marker = "OK" if ok else "FAIL"
        print(
            f"  {row['slug']:<16} {row['plan_tier']:<8} "
            f"{row['cw_acct']:<3} "
            f"{row['channels']}  {row['branches']}  "
            f"{row['secrets']}  {row['audience']}  {marker}"
        )
        if not ok:
            all_ok = False
            print(
                f"    expected: ch={expected.get('channels')} "
                f"br={expected.get('branches')} se={expected.get('secrets')} "
                f"au={expected.get('audience')}"
            )
    return all_ok


# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────


async def main(args: argparse.Namespace) -> int:
    db_url = (
        os.environ.get("POLARIS_DB_URL_SERVICE")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
    )
    if not db_url:
        print(
            "ERROR: POLARIS_DB_URL_SERVICE / DATABASE_URL is not set",
            file=sys.stderr,
        )
        return 1

    if not os.environ.get("POLARIS_KMS_KEY"):
        print(
            "ERROR: POLARIS_KMS_KEY is not set — refusing to seed without "
            "encryption key. See the workspace KMS runbook.",
            file=sys.stderr,
        )
        return 2

    tenants = _build_fixture()
    if args.tenant:
        tenants = [t for t in tenants if t.slug == args.tenant]
        if not tenants:
            print(f"ERROR: tenant slug {args.tenant!r} not in fixture",
                  file=sys.stderr)
            return 3

    ssl_mode = "require" if "supabase.co" in db_url else None
    conn = await asyncpg.connect(db_url, ssl=ssl_mode)
    try:
        # Teardown-only — delete the fixture tenants and exit without seeding.
        # Wired into the CI workflow as an `if: always()` step so a run never
        # leaves its e2e-* tenants behind in the (shared) DB.
        if args.teardown:
            print("Teardown: deleting E2E fixture tenants and exiting...")
            for t in tenants:
                await _delete_tenant(conn, t.id)
                print(f"  deleted {t.slug}")
            print(f"\nOK: {len(tenants)} fixture tenant(s) deleted.")
            return 0

        # Delete phase — all tenants first to avoid chatwoot_inbox_id unique
        # constraint conflicts when re-numbering inboxes across tenants.
        if args.reset:
            print("Deleting existing fixture rows...")
            for t in tenants:
                if t.chatwoot_account_id is not None:
                    stale = await conn.fetch(
                        "SELECT id FROM tenants WHERE chatwoot_account_id = $1"
                        " AND id != $2",
                        t.chatwoot_account_id, t.id,
                    )
                    for row in stale:
                        logger.info("  purging stale tenant %s (cw_acct=%s)",
                                    row["id"], t.chatwoot_account_id)
                        await _delete_tenant(conn, row["id"])
                await _delete_tenant(conn, t.id)

        for t in tenants:
            print(f"\n-> {t.slug} ({t.plan_tier})")
            await _upsert_tenant(conn, t)
            print("  tenant upserted")
            await _upsert_quotas(conn, t)
            print("  quotas upserted")
            await _upsert_channels(conn, t)
            print(f"  channels upserted ({len(t.channels)})")
            await _upsert_branches(conn, t)
            print(f"  branches upserted ({len(t.branches)})")
            await _upsert_secrets(conn, t)
            print(f"  secrets upserted ({len(t.secrets)})")
            await _upsert_admin_user(conn, t)
            print(f"  admin user '{t.admin_email}'")
            await _upsert_audience(conn, t)
            print(f"  audience upserted ({len(t.audience)})")

        # Knowledge: delegated to the existing seed script via a per-tenant
        # flag if/when it's extended. At this point we only seed the
        # structural rows. (Flow seeding was retired with flow_runtime —
        # audit Batch D, 2026-07-02; --skip-flows is now a no-op kept for
        # CLI backward-compat so existing invocations don't break.)
        if not args.skip_knowledge:
            print("Note: knowledge seeding skipped — invoke seed_demo_knowledge.py "
                  "per tenant once that script accepts --tenant-id.")

        # Final verification — only meaningful when ALL tenants seeded.
        if not args.tenant:
            slugs = [t.slug for t in _build_fixture()]
            ok = await _verify(conn, slugs)
            if not ok:
                print("\nFAIL: counts do not match expected fixture.",
                      file=sys.stderr)
                return 4
            print("\nOK: all 5 fixture tenants verified.")
        else:
            print(f"\nOK: {args.tenant} seeded.")
    finally:
        await conn.close()

    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--reset",
        action="store_true",
        help="Delete the 5 E2E tenants (or scoped via --tenant) before inserting.",
    )
    p.add_argument(
        "--teardown",
        action="store_true",
        help="Delete the E2E fixture tenants (or scoped via --tenant) and exit "
             "without seeding. Use as an end-of-run cleanup step.",
    )
    p.add_argument(
        "--tenant",
        choices=["polaris-owner", "tenant-a", "tenant-b", "tenant-c", "tenant-d"],
        help="Scope to one tenant. Default: seed all 5.",
    )
    p.add_argument(
        "--skip-flows", action="store_true",
        help="No-op (flow seeding was retired with flow_runtime — audit Batch D, "
             "2026-07-02). Kept for CLI backward-compat.",
    )
    p.add_argument(
        "--skip-knowledge", action="store_true",
        help="Skip knowledge seeding (delegated to seed_demo_knowledge.py).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(main(args)))
