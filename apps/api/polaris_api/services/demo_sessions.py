"""Demo session primitives — Demo System v3 (ADR-066).

Text-gated, single-account roleplay demo. A customer messages the operator's
live channels (all owned by the ``polaris-internal`` gateway tenant); by default
the bot answers as the Polaris sales assistant. When the customer types a
configured **demo code** (exact whole-token, case-insensitive) the bot switches
into roleplaying as that prospect's business — content scoped to a hidden brand
tenant (``is_demo=true``) with its own KB/persona/products — for a capped session.

The demo runtime (``services/demo_runtime.py``) calls these helpers to:
  * resolve a demo code -> hidden brand tenant (:func:`find_demo_tenant_by_code`),
  * enforce one-demo-ever-per-contact (:func:`has_ever_used_demo`),
  * read/track the active session + per-demo caps
    (:func:`get_active_demo_session`, :func:`activate_demo_session`,
    :func:`increment_demo_message`, :func:`expire_demo_session`).

Architecture note: all reads/writes go through the BYPASSRLS **service pool**.
Demo brand tenants are hidden tenants the operator owns, and resolving a
``demo_code`` -> brand tenant is a cross-tenant lookup that no single-tenant RLS
scope can satisfy. ``demo_sessions``' own RLS policy (migration 008b) stays in
place for any RLS-scoped path. This module is on the
``apps/api/scripts/lint_service_pool.py`` allowlist alongside ``webhook_resolver.py``.

Schema: migration 008b (``demo_sessions`` + ``polaris-internal`` seed) +
migration 114 (``tenants.is_demo`` / ``demo_duration_hours`` / ``demo_max_messages``;
``demo_sessions.lead_flagged_at``). Under v3, ``demo_sessions.tenant_id`` = the
BRAND tenant (was ``polaris-internal`` in the withdrawn channel-gated model).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from polaris_api.db.pools import get_service_pool

logger = logging.getLogger(__name__)

# The platform gateway tenant, seeded by migration 008b. Owns the operator's
# live channels + the default Polaris sales KB. Marked is_demo=true by migration
# 114 (so it's excluded from MRR/metrics/customer lists) but never resolves as a
# demo because its demo_code is NULL. Referenced as a constant so a future seed
# refactor can grep-find the call sites.
POLARIS_INTERNAL_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


# Baseline sales persona for the gateway, mirrored from migration 105. Kept here
# (not read from the .sql) so the recovery path is self-contained. THIS Python
# constant is the LIVING source — migration 105 is historical/immutable and will
# silently diverge if the persona ever changes, so edit it HERE, not in the .sql.
# Only applied when system_prompt is empty, so a later portal edit is never clobbered.
_POLARIS_INTERNAL_PERSONA = """You are Pol, the AI customer service assistant for Polaris.
Polaris is a multi-tenant AI customer-service chatbot platform for Malaysian small and medium businesses. You help prospects and customers understand what Polaris does across WhatsApp, Instagram, Messenger, Telegram, email, and the website widget.

<identity>
- You are an AI assistant, not a human. If a customer asks directly, say so honestly. Do not volunteer this unprompted.
- You represent Polaris. Think of yourself as a knowledgeable friend who happens to work there, warm and approachable, not a stiff corporate agent or a scripted chatbot.
</identity>

<tone_and_language>
- Core Vibe: Warm, helpful, and naturally conversational. A knowledgeable friend who works at Polaris, not a stiff corporate agent or a scripted chatbot.
- Language Mirroring (CRITICAL): Detect the customer's language and register in their very first message and reply in the exact same language and style throughout. Do not switch unless they switch first.
- English / Manglish: If they use English or Manglish, reply in natural Malaysian English. Local particles (lah, ya, bos, sis, kan) are fine ONLY if the customer uses them first.
- Bahasa Melayu: If they use Malay, reply in conversational Bahasa Melayu Pasar. Match a formal register if they write formally. Short forms (yg, nak, tak, boleh, dah) are natural.
- Mandarin: If they use Mandarin, reply in natural Simplified Mandarin as used locally in Malaysia. Keep brand, product names, and prices in their original form.
- Code-Switching: Malaysian customers often mix languages in one message. Match their exact mix naturally.
- Formal Register: Match a professional tone if the customer writes formally. Never reply casually to a formally written message.
</tone_and_language>

<what_polaris_does>
- Channels: WhatsApp, Instagram, Messenger, Telegram, email (Chatwoot-native), and a website launcher widget.
- Knowledge: the bot answers from each business's own ingested knowledge base (documents and URLs), with hybrid retrieval.
- Capabilities: appointment bookings (via Easy!Appointments), a product catalog with photos, broadcasts and approved templates, AI re-engagement follow-ups, and seamless handoff to a human agent in Chatwoot.
- Tiers: Starter, Pro, Ultra, and Enterprise, with different monthly bot-reply and booking quotas.
- Compliance: PDPA-aware; data export and retention controls are available.
</what_polaris_does>

<answering_rules>
- When several options could match, list ALL matching ones with names and starting prices, then ask which they mean. Never assume.
- Use exact figures from the provided context for pricing, quotas, and policy details. Zero approximation.
- When recommending a tier or feature and the provided context includes a URL, share the URL on a new line. Plain URL only.
</answering_rules>

<complaints_and_escalation>
- If the customer is upset, acknowledge their feelings sincerely before solving anything.
- For anything complex, undocumented, or when the customer asks for a person, let them know you will connect them with the team. Do not promise timing or agent availability.
- Never argue. Stay calm, empathetic, and solution-focused.
</complaints_and_escalation>"""


async def ensure_polaris_internal_gateway(conn: asyncpg.Connection) -> dict[str, bool]:
    """Idempotently (re)create the polaris-internal demo gateway tenant.

    Restores the row seeded by migration 008b + the ``is_demo`` flag from 114
    and, only when the persona is empty, the sales persona from migration 105.
    Safe to re-run. Exists because migrations are ``ON CONFLICT DO NOTHING`` and
    never re-run, so a deleted-or-overwritten gateway has no self-healing path.

    Must run on the BYPASSRLS **service pool** — it is a cross-tenant write to a
    fixed-UUID row. Returns ``{"persona_restored": bool}``.
    """
    await conn.execute(
        """
        INSERT INTO tenants (id, slug, status, plan_tier, timezone, is_demo)
        VALUES ($1, 'polaris-internal', 'active', 'enterprise', 'Asia/Kuala_Lumpur', TRUE)
        ON CONFLICT (id) DO UPDATE
           SET slug      = 'polaris-internal',
               status    = 'active',
               plan_tier = 'enterprise',
               timezone  = 'Asia/Kuala_Lumpur',
               is_demo   = TRUE
        """,
        POLARIS_INTERNAL_TENANT_ID,
    )
    restored_id = await conn.fetchval(
        """
        UPDATE tenants
           SET system_prompt = $2
         WHERE id = $1
           AND (system_prompt IS NULL OR btrim(system_prompt) = '')
        RETURNING id
        """,
        POLARIS_INTERNAL_TENANT_ID,
        _POLARIS_INTERNAL_PERSONA,
    )
    persona_restored = restored_id is not None
    logger.info(
        "ensure_polaris_internal_gateway: gateway row upserted, persona_restored=%s",
        persona_restored,
    )
    return {"persona_restored": persona_restored}


@dataclass(frozen=True)
class DemoTenant:
    """A hidden demo brand tenant resolved from a demo code."""

    tenant_id: UUID
    slug: str
    display_name: str
    demo_code: str
    duration_hours: int
    max_messages: int


@dataclass(frozen=True)
class ActiveDemoSession:
    """The contact's current non-expired demo session."""

    session_id: UUID
    tenant_id: UUID  # brand tenant (content scope)
    demo_code: str
    message_count: int
    max_messages: int
    expires_at: datetime
    chatwoot_conversation_id: int | None
    chatwoot_inbox_id: int | None


async def find_demo_tenant_by_code(token: str) -> DemoTenant | None:
    """Resolve an exact demo code (case-insensitive) to its brand tenant.

    Only ``is_demo`` tenants with a non-NULL ``demo_code`` match — the
    ``polaris-internal`` gateway (``demo_code`` IS NULL) never resolves here
    even though it is ``is_demo=true``. Returns None on no match or if the
    service pool isn't initialised yet (boot/isolated tests).
    """
    code = token.strip()
    if not code:
        return None
    try:
        pool = get_service_pool()
    except RuntimeError:
        logger.debug("find_demo_tenant_by_code: service pool not initialised")
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, slug, display_name, demo_code,
                   demo_duration_hours, demo_max_messages
              FROM tenants
             WHERE is_demo = true
               AND demo_code IS NOT NULL
               AND lower(demo_code) = lower($1)
               AND status <> 'deleted'
            """,
            code,
        )
    if row is None:
        return None
    return DemoTenant(
        tenant_id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"] or row["slug"],
        demo_code=row["demo_code"],
        duration_hours=int(row["demo_duration_hours"]),
        max_messages=int(row["demo_max_messages"]),
    )


async def has_ever_used_demo(channel_user_id: str) -> bool:
    """One-demo-ever lock: True if ANY ``demo_sessions`` row exists for this
    contact. Expired rows still count until the 90-day auto-purge sweep deletes
    them (ADR-038), so a contact gets exactly one brand demo per window.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT 1 FROM demo_sessions WHERE channel_user_id = $1 LIMIT 1",
            channel_user_id,
        )
    return val is not None


async def get_active_demo_session(channel_user_id: str) -> ActiveDemoSession | None:
    """Return the contact's non-expired demo session, or None."""
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, demo_code, message_count, max_messages,
                   expires_at, chatwoot_conversation_id, chatwoot_inbox_id
              FROM demo_sessions
             WHERE channel_user_id = $1
               AND is_expired = false
             ORDER BY activated_at DESC
             LIMIT 1
            """,
            channel_user_id,
        )
    if row is None:
        return None
    return ActiveDemoSession(
        session_id=row["id"],
        tenant_id=row["tenant_id"],
        demo_code=row["demo_code"],
        message_count=row["message_count"],
        max_messages=row["max_messages"],
        expires_at=row["expires_at"],
        chatwoot_conversation_id=row["chatwoot_conversation_id"],
        chatwoot_inbox_id=row["chatwoot_inbox_id"],
    )


async def activate_demo_session(
    *,
    channel: str,
    channel_user_id: str,
    channel_user_name: str | None,
    demo: DemoTenant,
    chatwoot_conversation_id: int | None = None,
    chatwoot_inbox_id: int | None = None,
) -> ActiveDemoSession:
    """Create a demo session bound to the brand tenant with its configured caps.

    ``expires_at = now + demo.duration_hours``; ``max_messages = demo.max_messages``.
    ``message_count`` starts at 0 — the activation/welcome message is not a demo
    turn. The trigger on ``expires_at`` (migration 008b) keeps ``auto_purge_at``
    in lock-step. Caller should have checked :func:`has_ever_used_demo` first.
    """
    pool = get_service_pool()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=demo.duration_hours)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO demo_sessions
                (tenant_id, channel, channel_user_id, channel_user_name,
                 demo_code, message_count, max_messages, activated_at, expires_at,
                 chatwoot_conversation_id, chatwoot_inbox_id)
            VALUES ($1, $2, $3, $4, $5, 0, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            demo.tenant_id,
            channel,
            channel_user_id,
            channel_user_name,
            demo.demo_code,
            demo.max_messages,
            now,
            expires_at,
            chatwoot_conversation_id,
            chatwoot_inbox_id,
        )
    assert row is not None
    logger.info(
        "demo_sessions: activated demo channel_user_id=%s code=%s tenant=%s",
        channel_user_id,
        demo.demo_code,
        demo.tenant_id,
    )
    return ActiveDemoSession(
        session_id=row["id"],
        tenant_id=demo.tenant_id,
        demo_code=demo.demo_code,
        message_count=0,
        max_messages=demo.max_messages,
        expires_at=expires_at,
        chatwoot_conversation_id=chatwoot_conversation_id,
        chatwoot_inbox_id=chatwoot_inbox_id,
    )


async def increment_demo_message(session_id: UUID) -> int:
    """Increment ``message_count`` for a demo session; return the new count."""
    pool = get_service_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            """
            UPDATE demo_sessions
               SET message_count = message_count + 1
             WHERE id = $1
            RETURNING message_count
            """,
            session_id,
        )
    return int(val) if val is not None else 0


async def expire_demo_session(session_id: UUID) -> None:
    """Mark a demo session expired — reverts the contact to the Polaris bot."""
    pool = get_service_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE demo_sessions SET is_expired = true WHERE id = $1",
            session_id,
        )


async def mark_lead_flagged(session_id: UUID) -> None:
    """Stamp ``lead_flagged_at`` (Demo System log marker; migration 114)."""
    pool = get_service_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE demo_sessions SET lead_flagged_at = now() WHERE id = $1",
            session_id,
        )


async def reset_visitor_demo(channel_user_id: str) -> int:
    """Admin action: delete all ``demo_sessions`` rows for a contact, clearing
    the one-ever lock so they can demo again. Returns the number of rows deleted.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM demo_sessions WHERE channel_user_id = $1",
            channel_user_id,
        )
    return int(result.split()[-1]) if result else 0


# ---------------------------------------------------------------------------
# Legacy gateway-channel lookup — retained for the not-yet-reworked seed/smoke
# scripts (M5). Takes an explicit phone_number_id arg; no demo-number config
# dependency. Safe to keep until the scripts migrate off it.
# ---------------------------------------------------------------------------

_DEMO_CHANNEL_ID_CACHE: UUID | None = None


async def get_demo_channel_id(*, phone_number_id: str) -> UUID | None:
    """Look up the polaris-internal channel id for a given phone_number_id.

    Returns None if the service pool isn't initialised or no matching
    ``tenant_channels`` row exists. Used by the smoke script to build an
    IncomingMessage without going through a webhook.
    """
    global _DEMO_CHANNEL_ID_CACHE  # noqa: PLW0603 — module-level cache by design
    if _DEMO_CHANNEL_ID_CACHE is not None:
        return _DEMO_CHANNEL_ID_CACHE
    try:
        pool = get_service_pool()
    except RuntimeError:
        logger.debug("get_demo_channel_id: service pool not initialised")
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM tenant_channels "
            "WHERE tenant_id = $1 AND phone_number_id = $2",
            POLARIS_INTERNAL_TENANT_ID,
            phone_number_id,
        )
    if row is None:
        return None
    _DEMO_CHANNEL_ID_CACHE = row["id"]
    return _DEMO_CHANNEL_ID_CACHE


def reset_demo_channel_cache_for_testing() -> None:
    """Clear the demo channel id cache (tests / re-seed mid-process)."""
    global _DEMO_CHANNEL_ID_CACHE  # noqa: PLW0603
    _DEMO_CHANNEL_ID_CACHE = None
