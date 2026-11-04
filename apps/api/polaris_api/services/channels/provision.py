"""Channel provisioning service — Phase 10 PR-0-retrofit.

Single entry point: ``provision_channel()``.

Enforces two tier-cap rules unless ``bypass_tier_cap=True``:

  1. ``allowed_channel_kinds`` check — the requested kind must be in the
     tenant's quota row. Starter = ``{'whatsapp'}``; Pro/Ultra/Enterprise
     = ``{'whatsapp', 'messenger', 'instagram_login', 'telegram', 'email'}``.

  2. Starter 1-inbox cap — Starter tenants may have at most 1 channel
     across all kinds (ADR-015 Amendment 3rd). Enforced via row COUNT so
     it survives an ``allowed_channel_kinds`` override that lets a Starter
     add WhatsApp after some future quota override.

When ``bypass_tier_cap=True`` (``platform_admin`` override) BOTH checks
are skipped. The caller is responsible for recording the audit row with
``action='channels_per_kind.override'``.

Callers must pass a connection that is already inside an open transaction
so the INSERT and the caller's audit row commit atomically.

``connection_payload`` required keys:
  - ``chatwoot_inbox_id`` (int)  — Chatwoot inbox already created by
    platform_admin via Chatwoot Platform API before calling this.

Optional keys (persisted when present):
  - ``waba_id``, ``phone_number_id``  — WhatsApp Business Account /
    phone number identifiers.
  - ``fb_page_id``                    — Messenger Facebook Page ID.
  - ``ig_business_account_id``        — Instagram Business Account ID.
  - ``wa_messaging_tier``             — WhatsApp messaging tier string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from polaris_api.services.channels.secret_kinds import BASE_CHANNEL_SECRET_KINDS

_GA_KINDS: frozenset[str] = frozenset({"whatsapp", "messenger"})
# Off-Meta channels M2 — Telegram is provisionable too (Pro+ gated via
# allowed_channel_kinds, same mechanism as IG/Messenger). instagram_login
# (graph.instagram.com; migration 104) is provisionable on the same footing as
# the FB-page 'instagram' channel. email (Chatwoot-native; migration 097) is
# provisionable too — it binds a tenant_channels row to a Chatwoot email inbox
# but stores NO outbound token (Chatwoot owns IMAP/SMTP), so it is absent from
# _CHANNEL_SECRET_KIND below; deprovision's secret cleanup .get()-skips it.
_PROVISIONABLE_KINDS: frozenset[str] = _GA_KINDS | {
    "telegram",
    "instagram_login",
    "email",
}
_STARTER_INBOX_CAP: int = 1

# Channel kind → the tenant_secrets.kind that holds its outbound token.
# Base map is shared (services.channels.secret_kinds); telegram is a local
# addition here since provision.py's scope (unlike lazy_provision.py /
# reconcile_channels.py) also covers the bespoke @BotFather telegram token.
# NOTE meta_fb_page_access_token is SHARED by instagram + messenger, so
# deprovision only deletes the secret when no other remaining channel still
# uses that same kind. instagram_login's token kind
# (meta_ig_login_access_token) is NOT shared, so deprovisioning it always
# removes the secret.
_CHANNEL_SECRET_KIND: dict[str, str] = {
    **BASE_CHANNEL_SECRET_KINDS,
    "telegram": "telegram_bot_token",
}


class TierCapError(Exception):
    """Raised when a channel add violates the tenant's tier limits.

    Callers should surface this as ``HTTP 403`` with the detail string.
    """


@dataclass(slots=True)
class ChannelProvisionResult:
    channel_id: UUID
    channel_kind: str
    tenant_id: UUID


@dataclass(slots=True)
class ChannelDeprovisionResult:
    channel_kind: str
    tenant_id: UUID
    # The tenant_secrets.kind that was deleted, or None when nothing was
    # removed (no secret for this kind, or a sibling channel still uses it).
    secret_kind_removed: str | None


async def provision_channel(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    channel_kind: str,
    connection_payload: dict[str, Any],
    bypass_tier_cap: bool = False,
) -> ChannelProvisionResult:
    """Insert a ``tenant_channels`` row and return its ID.

    Raises:
      ``TierCapError`` — tier cap violated and ``bypass_tier_cap=False``.
      ``ValueError``   — ``channel_kind`` not a GA kind, or
                         ``chatwoot_inbox_id`` missing from payload.
      ``asyncpg.UniqueViolationError`` — ``chatwoot_inbox_id`` already
                         in use (DB UNIQUE constraint on
                         ``tenant_channels.chatwoot_inbox_id``).
    """
    if channel_kind not in _PROVISIONABLE_KINDS:
        raise ValueError(
            f"channel_kind '{channel_kind}' is not a provisionable kind "
            f"(allowed: {sorted(_PROVISIONABLE_KINDS)})"
        )

    chatwoot_inbox_id = connection_payload.get("chatwoot_inbox_id")
    if chatwoot_inbox_id is None:
        raise ValueError("connection_payload must include 'chatwoot_inbox_id'")
    try:
        chatwoot_inbox_id = int(chatwoot_inbox_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"connection_payload.chatwoot_inbox_id must be an integer, "
            f"got {chatwoot_inbox_id!r}"
        ) from exc

    if not bypass_tier_cap:
        await _check_tier_cap(conn, tenant_id, channel_kind)

    channel_id: UUID = await conn.fetchval(
        """
        INSERT INTO tenant_channels (
          tenant_id,
          channel,
          chatwoot_inbox_id,
          waba_id,
          phone_number_id,
          fb_page_id,
          ig_business_account_id,
          wa_messaging_tier,
          telegram_bot_id,
          telegram_bot_username,
          telegram_webhook_secret,
          status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'active')
        RETURNING id
        """,
        tenant_id,
        channel_kind,
        chatwoot_inbox_id,
        connection_payload.get("waba_id"),
        connection_payload.get("phone_number_id"),
        connection_payload.get("fb_page_id"),
        connection_payload.get("ig_business_account_id"),
        connection_payload.get("wa_messaging_tier"),
        connection_payload.get("telegram_bot_id"),
        connection_payload.get("telegram_bot_username"),
        connection_payload.get("telegram_webhook_secret"),
    )

    return ChannelProvisionResult(
        channel_id=channel_id,
        channel_kind=channel_kind,
        tenant_id=tenant_id,
    )


async def deprovision_channel(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    channel_kind: str,
) -> ChannelDeprovisionResult:
    """Delete a tenant's channel row and its captured outbound-token secret.

    The secret is removed only when NO remaining channel for the tenant still
    uses the same ``tenant_secrets.kind`` — ``meta_fb_page_access_token`` is
    shared by instagram + messenger, so removing one must not strand the other.

    Callers must pass a connection already inside an open transaction so the
    DELETEs and the caller's audit row commit atomically.

    Raises:
      ``ValueError`` — ``channel_kind`` not provisionable, or the tenant has
                       no channel row of that kind (surface as 404/422).
    """
    if channel_kind not in _PROVISIONABLE_KINDS:
        raise ValueError(
            f"channel_kind '{channel_kind}' is not a provisionable kind "
            f"(allowed: {sorted(_PROVISIONABLE_KINDS)})"
        )

    deleted_id: UUID | None = await conn.fetchval(
        "DELETE FROM tenant_channels WHERE tenant_id = $1 AND channel = $2 "
        "RETURNING id",
        tenant_id,
        channel_kind,
    )
    if deleted_id is None:
        raise ValueError(
            f"tenant {tenant_id} has no '{channel_kind}' channel to remove"
        )

    secret_kind = _CHANNEL_SECRET_KIND.get(channel_kind)
    secret_kind_removed: str | None = None
    if secret_kind is not None:
        # Kinds that share this secret_kind (e.g. instagram + messenger).
        sibling_kinds = [
            k for k, v in _CHANNEL_SECRET_KIND.items() if v == secret_kind
        ]
        still_in_use: int = (
            await conn.fetchval(
                "SELECT COUNT(*) FROM tenant_channels "
                "WHERE tenant_id = $1 AND channel = ANY($2::text[])",
                tenant_id,
                sibling_kinds,
            )
            or 0
        )
        if still_in_use == 0:
            await conn.execute(
                "DELETE FROM tenant_secrets WHERE tenant_id = $1 AND kind = $2",
                tenant_id,
                secret_kind,
            )
            secret_kind_removed = secret_kind

    return ChannelDeprovisionResult(
        channel_kind=channel_kind,
        tenant_id=tenant_id,
        secret_kind_removed=secret_kind_removed,
    )


async def _check_tier_cap(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    channel_kind: str,
) -> None:
    """Raise ``TierCapError`` if the tier cap prevents this channel add.

    Two checks:
      1. ``allowed_channel_kinds`` membership.
      2. Starter 1-inbox total cap.
    """
    quotas = await conn.fetchrow(
        """
        SELECT plan_tier, allowed_channel_kinds
        FROM tenant_quotas
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if quotas is None:
        raise TierCapError(
            f"tenant_quotas row not found for tenant {tenant_id}; "
            "cannot enforce tier cap"
        )

    allowed: list[str] = list(quotas["allowed_channel_kinds"] or [])
    if channel_kind not in allowed:
        raise TierCapError(
            f"channel_kind '{channel_kind}' is not in "
            f"allowed_channel_kinds {allowed!r} "
            f"for plan_tier '{quotas['plan_tier']}'"
        )

    if quotas["plan_tier"] == "starter":
        count: int = await conn.fetchval(
            "SELECT COUNT(*) FROM tenant_channels WHERE tenant_id = $1",
            tenant_id,
        ) or 0
        if count >= _STARTER_INBOX_CAP:
            raise TierCapError(
                f"Starter plan allows at most {_STARTER_INBOX_CAP} inbox; "
                f"tenant already has {count}"
            )
