"""Shared seed helpers for the broadcast perf harness + smoke script.

Async functions only — no pytest fixtures. The smoke script imports
these directly; the perf test wraps them in pytest fixtures. Same
schema-write logic in both contexts so drift is structurally
impossible.

Mirrors the lightweight pattern from ``test_rls_phase_1.py``'s
``_seed_*`` helpers but extended for Phase 6's surface:

    tenant -> tenant_quotas -> tenant_channels -> tenant_templates
                            -> audience_members (bulk via COPY)

All inserts use the BYPASSRLS service pool — caller hands us an
``asyncpg.Connection`` from that pool. Audience seeding uses
``copy_records_to_table`` so 1000-row bulk inserts complete in
sub-second.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import asyncpg


async def seed_tenant(conn: asyncpg.Connection, *, slug: str) -> UUID:
    """Insert a minimal tenants row + the 1:1 tenant_quotas row.

    tenant_quotas is sized for Pro+: outbound_broadcast_enabled=true
    and monthly_broadcast_recipients large enough not to gate the
    perf test (the gate is exercised by unit tests in PR-5; here we
    care about throughput).
    """
    tenant_row = await conn.fetchrow(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id",
        slug,
    )
    assert tenant_row is not None  # noqa: S101
    tenant_id: UUID = tenant_row["id"]

    await conn.execute(
        """
        INSERT INTO tenant_quotas (
            tenant_id,
            bot_replies, vision_calls_per_month, conversation_retention_days,
            allowed_channel_kinds, outbound_broadcast_enabled, multi_branch_enabled,
            knowledge_chunks_total_mb, rate_limit_per_minute,
            monthly_llm_tokens_in, monthly_llm_tokens_out,
            monthly_ingestion_pages, monthly_broadcast_recipients,
            monthly_bookings, agent_seats_included, agent_seats_extra,
            bot_replies_period_cap
        ) VALUES (
            $1,
            25000, 0, 365,
            ARRAY['whatsapp','messenger']::text[], TRUE, TRUE,
            5000, 600,
            10000000, 10000000,
            10000, 1000000,
            5000, 1, 0,
            25000
        )
        """,
        tenant_id,
    )
    return tenant_id


async def seed_channel(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    chatwoot_inbox_id: int,
    phone_number_id: str = "PERF_PID",
    waba_id: str = "PERF_WABA",
    mm_lite_status: str | None = None,
) -> UUID:
    """Insert a tenant_channels row for WhatsApp + populate the IDs
    the worker reads when resolving sends."""
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_channels (
            tenant_id, channel, chatwoot_inbox_id,
            waba_id, phone_number_id, mm_lite_status, status
        ) VALUES ($1, 'whatsapp', $2, $3, $4, $5, 'active')
        RETURNING id
        """,
        tenant_id,
        chatwoot_inbox_id,
        waba_id,
        phone_number_id,
        mm_lite_status,
    )
    assert row is not None  # noqa: S101
    return row["id"]


async def seed_approved_template(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    tenant_channel_id: UUID,
    name: str = "perf_smoke_v1",
    category: str = "UTILITY",
) -> UUID:
    """Insert an approved tenant_templates row.

    Approved status is the precondition for broadcast.create_broadcast
    (PR-5). category defaults UTILITY so the 24h-window compliance
    check (PR-6) doesn't gate sends in the perf test.
    """
    components = [{"type": "BODY", "text": "Hello {{1}}, this is a test."}]
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_templates (
            tenant_id, tenant_channel_id, name, language, category,
            status, meta_template_id, components
        ) VALUES ($1, $2, $3, 'en_US', $4, 'approved', $5, $6::jsonb)
        RETURNING id
        """,
        tenant_id,
        tenant_channel_id,
        name,
        category,
        f"meta_tpl_{uuid4().hex[:16]}",
        json.dumps(components),
    )
    assert row is not None  # noqa: S101
    return row["id"]


async def seed_audience(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    count: int,
    channel: str = "whatsapp",
) -> int:
    """Bulk-insert ``count`` opted-in audience_members.

    Uses ``copy_records_to_table`` for sub-second 1000-row inserts.
    Each member gets a synthetic E.164-shaped phone derived from its
    sequence index so the (tenant_id, channel, channel_user_id)
    partial-UNIQUE doesn't collide.
    """
    records = []
    for i in range(count):
        # Use a fixed 60-prefix + zero-padded seq to keep records
        # E.164-shaped without touching libphonenumber here.
        phone = f"+60{i:010d}"
        records.append(
            (
                uuid4(),                              # id
                tenant_id,                            # tenant_id
                None,                                 # tenant_branch_id
                channel,                              # channel
                phone,                                # channel_user_id
                f"Perf User {i}",                     # channel_user_name
                phone,                                # phone_number_e164
                None,                                 # email
                None,                                 # bsuid
                "en-MY",                              # locale
                [],                                   # tags
                "api_sync",                           # opt_in_source
                None,                                 # opted_out_reason
                None,                                 # opted_out_at_channel
                None,                                 # chatwoot_contact_id
                "{}",                                 # metadata (JSON text)
            )
        )

    await conn.copy_records_to_table(
        "audience_members",
        records=records,
        columns=[
            "id",
            "tenant_id",
            "tenant_branch_id",
            "channel",
            "channel_user_id",
            "channel_user_name",
            "phone_number_e164",
            "email",
            "bsuid",
            "locale",
            "tags",
            "opt_in_source",
            "opted_out_reason",
            "opted_out_at_channel",
            "chatwoot_contact_id",
            "metadata",
        ],
    )
    return count


async def cleanup_tenant(conn: asyncpg.Connection, *, tenant_id: UUID) -> None:
    """Delete a tenant + everything it owns.

    audience_members.tenant_id has no ON DELETE CASCADE (migration 009),
    so we delete it explicitly first; all other Phase 6 tables cascade.
    """
    # Several tables reference tenants without ON DELETE CASCADE; delete them
    # explicitly in dependency order before the parent tenant row.
    #   broadcast_recipients.audience_member_id → audience_members (no CASCADE)
    #   audit_logs.tenant_id → tenants (no CASCADE; written by broadcast worker)
    #   audience_members.tenant_id → tenants (no CASCADE)
    #   demo_sessions + tenant_billing → tenants (no CASCADE; may be empty, safe)
    # All other Phase 6 tables (broadcasts, tenant_templates, …) have CASCADE.
    await conn.execute("DELETE FROM broadcast_recipients WHERE tenant_id = $1", tenant_id)
    await conn.execute("DELETE FROM audit_logs WHERE tenant_id = $1", tenant_id)
    await conn.execute("DELETE FROM audience_members WHERE tenant_id = $1", tenant_id)
    await conn.execute("DELETE FROM demo_sessions WHERE tenant_id = $1", tenant_id)
    await conn.execute("DELETE FROM tenant_billing WHERE tenant_id = $1", tenant_id)
    await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
