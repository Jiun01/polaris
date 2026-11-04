"""Webhook tenant resolver — Phase 2 PR-2.

Purpose
-------
At webhook delivery time we have **no** ``TenantContext`` — the request is
unauthenticated by design (Meta / Chatwoot just POST). We must figure out
which ``tenants`` row this delivery belongs to before opening any
tenant-scoped (RLS-enforced) query.

This module owns the **only Phase 2 use** of the BYPASSRLS service pool,
which is what makes the pre-resolution lookup possible. The lint at
``apps/api/scripts/lint_service_pool.py`` enforces the allowlist; modules
under ``polaris_api/webhooks/`` import nothing from here directly except
the pure-data ``ResolvedChannel`` dataclass + the two ``resolve_*``
coroutines + ``record_event``.

What this module owns
---------------------

* ``resolve_meta(entry_id, kind)`` — match an inbound Meta ``entry.id``
  against the appropriate ``tenant_channels`` column (``waba_id`` /
  ``ig_business_account_id`` / ``fb_page_id``). Honours ADR-015
  multiplicity: a tenant may have **N** rows per channel kind; the
  resolver returns the **specific row**, not just the tenant id.

* ``record_event(...)`` — insert one ``webhook_events`` row. Used by
  both branches (resolved + unresolved); the row's ``tenant_id`` is
  ``NULL`` for hmac_fail / tenant_not_found.

* ``ResolvedChannel`` — frozen dataclass returned to the router. Carries
  the bits the router needs without re-querying.

PR-5 will add ``resolve_chatwoot(inbox_id: int)`` next to ``resolve_meta``;
the surface is intentionally parallel so the router code can switch
shapes via the ``ChannelKind`` literal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from polaris_api.db.pools import get_service_pool

logger = logging.getLogger(__name__)


ChannelKind = Literal[
    "whatsapp", "messenger", "chatwoot", "telegram", "email",
    # Instagram-API-with-Instagram-Login (graph.instagram.com). The legacy
    # FB-page 'instagram' channel (Messenger-API-for-Instagram) was dropped in
    # the Chatwoot-native rehaul — migration 109.
    "instagram_login",
]
MetaResolutionKind = Literal["waba", "ig", "fb"]
WebhookSource = Literal[
    "meta",
    "chatwoot",
    # Phase 6 — separate sources per Meta webhook surface; rationale in
    # migration 046_webhook_events_source_expansion.sql header. Each owns
    # its own idempotency-key shape, so collapsing them under 'meta' would
    # make (source, external_id) UNIQUE less meaningful.
    "meta-template-status",
    "wa-status",
    "wa-account",
    # Off-Meta channels M1 — Telegram inbound (migration 094). Per-bot
    # idempotency keyed on (channel_id, update_id); its own source value
    # keeps (source, external_id) UNIQUE meaningful, same rationale as above.
    "telegram",
]
WebhookEventStatus = Literal[
    "received",
    "processed",
    "tenant_not_found",
    "suspended_quota",
    "suspended_payment",
    "suspended_deleted",
    "replay_ignored",
    "demo_routed",
    "hmac_fail",
    "non_message_event",  # PR-7 — resolved tenant + envelope had no inbound user messages
    "human_handling",  # rehaul Stage 1 — bot gated: a human owns the conversation
    "no_recipient",  # rehaul Stage 1 — could not resolve the reply recipient
    "chatwoot_deferred",  # rehaul Stage 4 — use_chatwoot_inbound on: meta_router defers to Chatwoot
]


@dataclass(frozen=True)
class ResolvedChannel:
    """One specific ``tenant_channels`` row that owns a webhook delivery.

    Per ADR-015, a tenant may have multiple channel rows of the same
    kind (e.g. two WhatsApp numbers under the same WABA). The router
    cares which **row** owns this delivery, not just which tenant.
    """

    tenant_id: UUID
    channel_id: UUID
    channel_kind: ChannelKind
    chatwoot_inbox_id: int


# Map the Meta-side resolution kind to the column it joins on. Keeps the
# SQL string in one place and the caller-facing API readable.
_META_KIND_TO_COLUMN: dict[MetaResolutionKind, str] = {
    "waba": "waba_id",
    "ig": "ig_business_account_id",
    "fb": "fb_page_id",
}
# Fallback kind→channel map. The resolver now returns the resolved row's own
# ``channel`` column (so 'instagram' vs 'instagram_login' — which share the
# ``ig_business_account_id`` lookup — stay distinguishable). This map is only
# used as a defensive fallback when a matched row carries a ``channel`` value
# that isn't a valid Meta surface (a data bug; logged loudly).
_META_KIND_TO_CHANNEL: dict[MetaResolutionKind, ChannelKind] = {
    "waba": "whatsapp",
    "ig": "instagram_login",
    "fb": "messenger",
}
# Channel-column values reachable via a Meta entry-id lookup. 'instagram' and
# 'instagram_login' both resolve through ``ig_business_account_id``.
_META_CHANNEL_KINDS: frozenset[str] = frozenset(
    {"whatsapp", "instagram", "messenger", "instagram_login"}
)


@dataclass(frozen=True)
class ResolvedTenant:
    """Minimal tenant resolution for non-inbox webhook events (e.g. Chatwoot
    ``conversation_status_changed`` / ``label_updated``) where no inbox id is
    available in the payload — only the Chatwoot ``account.id``."""

    tenant_id: UUID
    chatwoot_account_id: int


@dataclass(frozen=True)
class AgentForwardContext:
    """All credentials needed to forward a human-agent CW reply to the customer.

    Returned by ``resolve_agent_forward_context``. Field set depends on
    ``channel``:
      - whatsapp → ``phone_number_id`` + ``waba_token`` (→ MetaWhatsAppClient)
      - instagram / messenger → ``page_token`` (→ MetaMessagingClient.send_text);
        ``channel_user_id`` is the IGSID / PSID
      - instagram_login → ``ig_login_token`` (→ InstagramLoginClient.send_text on
        graph.instagram.com); ``channel_user_id`` is the IGSID (migration 104)
      - telegram → ``telegram_token`` (→ TelegramClient.send_message);
        ``channel_user_id`` is the Telegram chat id (off-Meta channels M5)
    """

    channel: str
    channel_user_id: str
    phone_number_id: str | None = None
    waba_token: str | None = None
    telegram_token: str | None = None
    page_token: str | None = None
    ig_login_token: str | None = None


async def resolve_by_chatwoot_account_id(account_id: int) -> ResolvedTenant | None:
    """Return the tenant whose ``chatwoot_account_id`` matches ``account_id``.

    ``chatwoot_account_id`` has a UNIQUE constraint on ``tenants``
    (migration 001) — at-most-one by construction.

    Returns ``None`` when no match; the caller should silently ack 204.
    Raises ``asyncpg`` errors on DB outage — map to 503 so Chatwoot retries.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, chatwoot_account_id FROM tenants WHERE chatwoot_account_id = $1",
            account_id,
        )
    if row is None:
        return None
    return ResolvedTenant(
        tenant_id=row["id"],
        chatwoot_account_id=row["chatwoot_account_id"],
    )


async def resolve_agent_forward_context(
    tenant_id: UUID,
    chatwoot_conversation_id: int,
) -> AgentForwardContext | None:
    """Fetch the credentials to forward a human-agent CW message to the customer.

    Supports WhatsApp (AC-12-6), Instagram/Messenger, and Telegram (off-Meta
    channels M5):
      1. conversations lookup → channel + channel_user_id
      2. WhatsApp: tenant_channels → phone_number_id; tenant_secrets →
         meta_waba_access_token.
         Instagram/Messenger: tenant_secrets → meta_fb_page_access_token (the
         IGSID / PSID is the channel_user_id; no per-channel id lookup needed).
         Telegram: tenant_secrets → telegram_bot_token (chat id is the
         channel_user_id; no per-channel id lookup needed).

    Returns ``None`` when any piece is unavailable, or the channel is not one
    of WhatsApp / Instagram / Messenger / Telegram. Raises ``asyncpg`` errors
    on DB outage so the router can map them to 503.
    """
    # Import here to avoid a circular dependency: secrets → config → (indirectly) pools.
    from polaris_api.services.secrets import get as _secrets_get  # noqa: PLC0415

    pool = get_service_pool()
    async with pool.acquire() as conn:
        conv_row = await conn.fetchrow(
            """
            SELECT channel, channel_user_id, channel_id
              FROM conversations
             WHERE tenant_id = $1 AND chatwoot_conversation_id = $2
             LIMIT 1
            """,
            tenant_id,
            chatwoot_conversation_id,
        )
        if conv_row is None:
            return None

        channel = str(conv_row["channel"])
        channel_user_id = str(conv_row["channel_user_id"])
        conv_channel_id = conv_row["channel_id"]  # tenant_channels.id, may be NULL

        if channel == "telegram":
            tg_token = await _secrets_get(conn, tenant_id, "telegram_bot_token")
            if tg_token is None:
                logger.warning(
                    "resolve_agent_forward_context: no telegram token for tenant=%s",
                    tenant_id,
                )
                return None
            return AgentForwardContext(
                channel="telegram",
                channel_user_id=channel_user_id,
                telegram_token=tg_token.decode("utf-8"),
            )

        if channel in ("messenger",):
            page_token = await _secrets_get(
                conn, tenant_id, "meta_fb_page_access_token"
            )
            if page_token is None:
                logger.warning(
                    "resolve_agent_forward_context: no page token for "
                    "tenant=%s channel=%s",
                    tenant_id,
                    channel,
                )
                return None
            return AgentForwardContext(
                channel=channel,
                channel_user_id=channel_user_id,
                page_token=page_token.decode("utf-8"),
            )

        if channel == "instagram_login":
            ig_login_token = await _secrets_get(
                conn, tenant_id, "meta_ig_login_access_token"
            )
            if ig_login_token is None:
                logger.warning(
                    "resolve_agent_forward_context: no IG-login token for "
                    "tenant=%s",
                    tenant_id,
                )
                return None
            return AgentForwardContext(
                channel="instagram_login",
                channel_user_id=channel_user_id,
                ig_login_token=ig_login_token.decode("utf-8"),
            )

        if channel != "whatsapp":
            logger.debug(
                "resolve_agent_forward_context: unsupported channel %r cw_conv=%s — skip",
                channel,
                chatwoot_conversation_id,
            )
            return None

        phone_number_id: str | None
        if conv_channel_id is not None:
            # Pin to the SAME WABA the customer messaged (ADR-015 multiplicity):
            # the conversation records the inbound channel_id since migration
            # 106, so a multi-WABA tenant forwards the agent reply from the
            # correct number rather than an arbitrary/oldest one.
            phone_number_id = await conn.fetchval(
                """
                SELECT phone_number_id
                  FROM tenant_channels
                 WHERE id = $1
                   AND tenant_id = $2
                   AND channel = 'whatsapp'
                   AND phone_number_id IS NOT NULL
                 LIMIT 1
                """,
                conv_channel_id,
                tenant_id,
            )
        else:
            # Legacy conversation created before conversations.channel_id landed
            # — no pinned WABA. Fall back to the oldest active row: deterministic
            # and correct for the single-WABA common case.
            phone_number_id = await conn.fetchval(
                """
                SELECT phone_number_id
                  FROM tenant_channels
                 WHERE tenant_id = $1
                   AND channel = 'whatsapp'
                   AND phone_number_id IS NOT NULL
                 ORDER BY created_at ASC
                 LIMIT 1
                """,
                tenant_id,
            )
        if not phone_number_id:
            logger.warning(
                "resolve_agent_forward_context: no phone_number_id for tenant=%s",
                tenant_id,
            )
            return None

        token_bytes = await _secrets_get(conn, tenant_id, "meta_waba_access_token")
        if token_bytes is None:
            logger.warning(
                "resolve_agent_forward_context: no WABA token for tenant=%s", tenant_id
            )
            return None

    return AgentForwardContext(
        channel=channel,
        channel_user_id=channel_user_id,
        phone_number_id=str(phone_number_id),
        waba_token=token_bytes.decode("utf-8"),
    )


async def resolve_meta(
    entry_id: str,
    kind: MetaResolutionKind,
) -> ResolvedChannel | None:
    """Return the ``tenant_channels`` row that owns ``entry_id``.

    Looks up the per-kind column with a single indexed query against the
    BYPASSRLS service pool. Returns ``None`` when no row matches — the
    router writes a ``tenant_not_found`` ``webhook_events`` row and acks
    204 in that case.

    ADR-015 multiplicity: ``(channel, waba_id)`` is non-unique in
    schema; in practice each WABA is owned by one tenant, but we don't
    rely on a unique constraint. The query returns at most one row
    because the WABA → tenant mapping is enforced by the channel
    onboarding flow (Phase 8 portal). If a second row appears it's a
    data bug; we log + return the first deterministically (ordered by
    ``id`` so the choice is stable).

    Returns ``None`` on no match. Raises ``asyncpg`` errors on DB outage
    — the router maps those to HTTP 503 so Meta retries.
    """
    column = _META_KIND_TO_COLUMN.get(kind)
    if column is None:
        # Defensive: the Literal type would normally prevent this, but a
        # caller passing a runtime-derived string shouldn't be able to
        # injection-style our SQL.
        raise ValueError(f"unknown meta kind: {kind!r}")

    pool = get_service_pool()
    async with pool.acquire() as conn:
        # We can't use a bind parameter for the column name; the kind is
        # an enum we control (Literal) so concatenation is safe. The
        # entry_id is bound as $1.
        rows = await conn.fetch(
            f"""
            SELECT id, tenant_id, channel, chatwoot_inbox_id
              FROM tenant_channels
             WHERE {column} = $1
               AND status = 'active'
             ORDER BY id
             LIMIT 2
            """,  # noqa: S608 — column name is from a closed enum, not user input
            entry_id,
        )

    if not rows:
        return None

    if len(rows) > 1:
        # Data inconsistency surface — keep the dispatcher behaviour
        # deterministic + log loudly so ops notices. (For kind='ig' this also
        # covers the unlikely case of an 'instagram' and an 'instagram_login'
        # row sharing one ig_business_account_id; first-by-id wins stably.)
        logger.warning(
            "tenant_channels: multiple rows matched %s=%s; picking first by id",
            column,
            entry_id,
        )

    row = rows[0]
    # Return the row's OWN channel so the dispatcher can tell 'instagram' (FB
    # page → graph.facebook.com) from 'instagram_login' (graph.instagram.com).
    # The WHERE clause on the per-kind column guarantees this value is
    # consistent with `kind`; fall back to the static map on an unexpected
    # value (a data bug).
    channel_kind: ChannelKind = row["channel"]
    if channel_kind not in _META_CHANNEL_KINDS:
        logger.warning(
            "resolve_meta: unexpected channel %r for %s=%s; using kind default",
            channel_kind,
            column,
            entry_id,
        )
        channel_kind = _META_KIND_TO_CHANNEL[kind]
    return ResolvedChannel(
        tenant_id=row["tenant_id"],
        channel_id=row["id"],
        channel_kind=channel_kind,
        chatwoot_inbox_id=row["chatwoot_inbox_id"],
    )


async def resolve_chatwoot(inbox_id: int) -> ResolvedChannel | None:
    """Return the ``tenant_channels`` row whose ``chatwoot_inbox_id`` matches.

    Phase 2 PR-5. ``chatwoot_inbox_id`` has a UNIQUE constraint on
    ``tenant_channels`` (per migration 004), so this is at-most-one
    by construction — no multiplicity branch needed.

    ``channel_kind`` returned is the row's ``channel`` column —
    'whatsapp'/'instagram'/'instagram_login'/'messenger' (the Meta surfaces),
    'telegram' (Chatwoot-native ``Channel::Telegram`` inbox — Chatwoot owns the
    Telegram bot connection and fires the agent-bot webhook just like the Meta
    surfaces), or 'email' (Chatwoot-native email inbox). Phase 7 reads this to
    pick the right outbound API call; the email-assist path reads it to know an
    inbound is an email it should draft a reply for.

    Telegram is a native Chatwoot channel: its bot webhook points at Chatwoot
    (not Polaris), so inbound arrives here through the agent-bot path like the
    Meta surfaces — hence 'telegram' is an accepted kind below. The old
    own-webhook router (``telegram_router`` + ``resolve_telegram``) was deleted.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, channel, chatwoot_inbox_id
              FROM tenant_channels
             WHERE chatwoot_inbox_id = $1
               AND status = 'active'
            """,
            inbox_id,
        )

    if row is None:
        return None
    channel_kind = row["channel"]
    if channel_kind not in {
        "whatsapp", "instagram", "instagram_login", "messenger", "telegram", "email"
    }:
        # Defensive: Chatwoot inboxes back the Meta surfaces (incl.
        # instagram_login), the native Telegram channel, plus Chatwoot-native
        # email. Any other kind here is not a deliberate Chatwoot routing
        # surface, so the dispatcher must opt it in explicitly.
        logger.warning(
            "resolve_chatwoot: unexpected channel kind %s for inbox_id=%s",
            channel_kind,
            inbox_id,
        )
        return None
    return ResolvedChannel(
        tenant_id=row["tenant_id"],
        channel_id=row["id"],
        channel_kind=channel_kind,
        chatwoot_inbox_id=row["chatwoot_inbox_id"],
    )


async def resolve_chatwoot_with_status(
    inbox_id: int,
) -> tuple[ResolvedChannel | None, str | None, str | None]:
    """Resolve the channel row AND the owning tenant's status in ONE round-trip.

    Combines ``resolve_chatwoot`` + ``fetch_tenant_status`` so the agent-bot
    webhook hot path makes a single service-pool query instead of two sequential
    ``pool.acquire()`` calls — trimming the per-message latency that, under
    pooler pressure, could push the handler past Chatwoot's webhook timeout
    (which evicts the conversation from bot mode).

    Behaviour matches calling the two separately:
      * ``(None, None, None)`` when no ACTIVE ``tenant_channels`` row matches
        ``inbox_id``, OR the matched row's channel kind is not a deliberate
        Chatwoot routing surface — the router treats a ``None`` channel as
        ``tenant_not_found`` (204), same as ``resolve_chatwoot`` returning None.
      * ``(ResolvedChannel, status, suspended_reason)`` otherwise. The LEFT JOIN
        means a (FK-impossible) missing tenant yields ``status=None`` →
        ``DROP_DELETED`` at the gate, exactly like ``fetch_tenant_status``.

    Raises ``asyncpg`` errors on DB outage so the router maps them to a
    retryable 5xx.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tc.id                 AS channel_id,
                   tc.tenant_id          AS tenant_id,
                   tc.channel            AS channel,
                   tc.chatwoot_inbox_id  AS chatwoot_inbox_id,
                   t.status::text        AS tenant_status,
                   t.suspended_reason    AS suspended_reason
              FROM tenant_channels tc
              LEFT JOIN tenants t ON t.id = tc.tenant_id
             WHERE tc.chatwoot_inbox_id = $1
               AND tc.status = 'active'
            """,
            inbox_id,
        )

    if row is None:
        return None, None, None

    channel_kind = row["channel"]
    if channel_kind not in {
        "whatsapp", "instagram", "instagram_login", "messenger", "telegram", "email"
    }:
        # Same defensive gate as resolve_chatwoot: any other kind is not a
        # deliberate Chatwoot routing surface — treat as no match.
        logger.warning(
            "resolve_chatwoot_with_status: unexpected channel kind %s for inbox_id=%s",
            channel_kind,
            inbox_id,
        )
        return None, None, None

    resolved = ResolvedChannel(
        tenant_id=row["tenant_id"],
        channel_id=row["channel_id"],
        channel_kind=channel_kind,
        chatwoot_inbox_id=row["chatwoot_inbox_id"],
    )
    return resolved, row["tenant_status"], row["suspended_reason"]


async def fetch_tenant_status(tenant_id: UUID) -> tuple[str | None, str | None]:
    """Return ``(status, suspended_reason)`` for ``tenant_id``.

    Lives next to ``resolve_meta`` because it shares the same need: a
    BYPASSRLS read before any TenantContext is established. The webhook
    dispatcher calls this immediately after the resolver hands back a
    ``ResolvedChannel``, then feeds the tuple into
    ``webhooks._status_gate.decide`` (which is pure-data + lives under
    ``webhooks/`` because it has no DB dependency).

    Returns ``(None, None)`` if the tenant row vanished mid-flight (the
    resolver just touched it — concurrent hard-delete in the same ms is
    the only way this fires). The status gate treats that as
    ``DROP_DELETED``.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, suspended_reason "
            "FROM tenants WHERE id = $1",
            tenant_id,
        )

    if row is None:
        logger.warning("fetch_tenant_status: tenants row missing for %s", tenant_id)
        return None, None
    return row["status"], row["suspended_reason"]


async def record_event(
    *,
    source: WebhookSource,
    external_id: str,
    status: WebhookEventStatus,
    payload_hash: bytes,
    tenant_id: UUID | None = None,
    channel_id: UUID | None = None,
    error_class: str | None = None,
) -> UUID:
    """Insert a ``webhook_events`` row. Returns the new row id.

    Goes through the BYPASSRLS service pool because:

    * unresolved-tenant rows (``hmac_fail`` / ``tenant_not_found``) carry
      ``tenant_id IS NULL`` — the per-tenant INSERT policy on
      ``webhook_events`` would block them via ``polaris_anon``;
    * resolved rows could go via ``polaris_anon`` (PR-4 will do that
      from within a TenantContext tx) but PR-2 keeps both paths through
      the same writer so the router's call site stays minimal.

    Idempotency: the table's ``UNIQUE (source, external_id)`` constraint
    catches re-deliveries. We use ``ON CONFLICT DO NOTHING`` so the
    second insert is a silent no-op + we still return the **existing**
    row id so the caller can observe the dedup without an extra round
    trip.

    ``payload_hash`` must be exactly 32 bytes (SHA-256). The DB CHECK
    enforces this; raising in app code first gives a nicer error.
    """
    if len(payload_hash) != 32:
        raise ValueError(
            f"payload_hash must be 32 bytes (sha256); got {len(payload_hash)}"
        )

    new_id = uuid4()
    pool = get_service_pool()
    async with pool.acquire() as conn:
        # RETURNING id rather than computed-client-side because
        # ON CONFLICT DO NOTHING returns no row — the COALESCE pattern
        # makes the existing row's id reachable in one query.
        row = await conn.fetchrow(
            """
            WITH ins AS (
              INSERT INTO webhook_events
                  (id, source, external_id, tenant_id, channel_id,
                   status, error_class, payload_hash)
              VALUES
                  ($1, $2, $3, $4, $5, $6, $7, $8)
              ON CONFLICT (source, external_id) DO NOTHING
              RETURNING id
            )
            SELECT id FROM ins
            UNION ALL
            SELECT id FROM webhook_events
             WHERE source = $2 AND external_id = $3
             LIMIT 1
            """,
            new_id,
            source,
            external_id,
            tenant_id,
            channel_id,
            status,
            error_class,
            payload_hash,
        )

    # The CTE either inserts (returns new_id) or selects the existing
    # row's id. asyncpg always returns at least one row.
    if row is None:
        raise RuntimeError("webhook_events insert/select CTE produced no row — this is a bug")

    row_id: UUID = row["id"]
    return row_id
