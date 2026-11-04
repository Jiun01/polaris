"""Lazy channel provisioning on first inbound — Chatwoot-native rehaul follow-up.

When the agent-bot webhook receives a message for an inbox that has NO
``tenant_channels`` row, this service registers the channel on the fly using the
Chatwoot service token Polaris already holds — so a sales-led / App-Review flow
never needs a manual ``POST /api/admin/tenants/{id}/channels`` step to make the
bot reply.

Provisioning RESPECTS tenant entitlements: a kind not in the tenant's
``allowed_channel_kinds`` is reported as ``disallowed`` (the router skips +
alerts + negative-caches), never silently bypassed. Scope is limited to the
three kinds whose access token can be auto-captured from Chatwoot — whatsapp,
messenger, instagram_login. telegram (bespoke @BotFather token via the
connect_telegram flow) and email (Chatwoot-native, its own connect flow, not in
provision._PROVISIONABLE_KINDS) are out of scope and fall through to the
existing ``tenant_not_found`` 204.

Architectural rule (``chatwoot_router.py``): the webhook router does no raw
BYPASSRLS DB work — all of it lives here behind ``get_service_pool()``, and the
router only calls ``lazy_provision_channel()``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from polaris_api.db.pools import get_service_pool
from polaris_api.services import secrets as secrets_svc
from polaris_api.services.audit import record as audit_record
from polaris_api.services.channels.provision import TierCapError, provision_channel
from polaris_api.services.channels.secret_kinds import BASE_CHANNEL_SECRET_KINDS
from polaris_api.services.chatwoot.agent_bot import attach_inbox_agent_bot
from polaris_api.services.chatwoot.client import ChatwootError, get_chatwoot_client
from polaris_api.services.webhook_resolver import (
    ResolvedChannel,
    resolve_by_chatwoot_account_id,
    resolve_chatwoot_with_status,
)

logger = logging.getLogger(__name__)

# Chatwoot channel_type -> Polaris channel kind. Keep in sync with
# admin_tenants._CW_KIND_TO_POLARIS (local copy avoids importing the admin
# router into a service, which would risk an import cycle).
_CW_KIND_TO_POLARIS: dict[str, str] = {
    "Channel::Whatsapp": "whatsapp",
    "Channel::Instagram": "instagram_login",
    "Channel::FacebookPage": "messenger",
    "Channel::Telegram": "telegram",
    "Channel::Email": "email",
}

# The kinds lazy provisioning handles — exactly those whose access token can be
# auto-captured from Chatwoot. Keep in sync with
# admin_tenants._TOKEN_CHANNEL_KINDS.
_LAZY_PROVISIONABLE_KINDS: frozenset[str] = frozenset(
    {"whatsapp", "messenger", "instagram_login"}
)

# Polaris channel kind -> tenant_secrets.kind for the captured access token.
# See services.channels.secret_kinds for the shared base map.
_CHANNEL_TO_SECRET_KIND = BASE_CHANNEL_SECRET_KINDS

LazyOutcome = Literal["provisioned", "disallowed", "no_tenant", "not_applicable"]


@dataclass(frozen=True, slots=True)
class LazyProvisionResult:
    outcome: LazyOutcome
    resolved: ResolvedChannel | None = None
    tenant_status: str | None = None
    suspended_reason: str | None = None
    channel_kind: str | None = None


def _extract_meta_ids(inbox: dict[str, Any]) -> dict[str, Any]:
    """Pull the channel-specific ids out of a Chatwoot inbox object.

    Mirrors ``admin_tenants.get_inbox_info`` — WA Cloud ids live in
    ``provider_config`` for API-provisioned inboxes and ``additional_attributes``
    for dashboard-created ones.
    """
    provider_config = inbox.get("provider_config") or {}
    additional_attrs = inbox.get("additional_attributes") or {}

    def _wa(key: str) -> str | None:
        val = provider_config.get(key) or additional_attrs.get(key)
        return str(val) if val else None

    payload: dict[str, Any] = {}
    waba_id = _wa("business_account_id")
    phone_number_id = _wa("phone_number_id")
    if waba_id:
        payload["waba_id"] = waba_id
    if phone_number_id:
        payload["phone_number_id"] = phone_number_id
    if inbox.get("instagram_id"):
        payload["ig_business_account_id"] = str(inbox["instagram_id"])
    if inbox.get("page_id"):
        payload["fb_page_id"] = str(inbox["page_id"])
    return payload


async def lazy_provision_channel(
    *, account_id: int, inbox_id: int
) -> LazyProvisionResult:
    """Provision a Chatwoot inbox into Polaris on first inbound. See module doc.

    The router maps the returned outcome:
      - ``provisioned``   -> continue processing with ``.resolved``/``.tenant_status``
      - ``disallowed``    -> skip + alert + negative-cache, 204
      - ``no_tenant``     -> existing ``tenant_not_found`` 204
      - ``not_applicable``-> existing ``tenant_not_found`` 204 (telegram/email/unknown)

    Raises ``asyncpg.PostgresError`` on a DB outage so the router maps it to a
    retryable 5xx (Chatwoot redelivers; provisioning is idempotent via the
    ``chatwoot_inbox_id`` UNIQUE constraint). A Chatwoot API blip is swallowed to
    ``not_applicable`` (the next message retries) rather than 5xx-ing.
    """
    tenant = await resolve_by_chatwoot_account_id(account_id)
    if tenant is None:
        return LazyProvisionResult(outcome="no_tenant")

    # Discover the inbox kind + ids from Chatwoot via the service token.
    try:
        inboxes = await get_chatwoot_client().list_inboxes(account_id=account_id)
    except ChatwootError as exc:
        logger.warning(
            "lazy_provision: list_inboxes failed account=%s inbox=%s err=%s",
            account_id,
            inbox_id,
            type(exc).__name__,
        )
        return LazyProvisionResult(outcome="not_applicable")

    inbox = next((i for i in inboxes if i.get("id") == inbox_id), None)
    if inbox is None:
        logger.warning(
            "lazy_provision: inbox %s not in account %s inbox list",
            inbox_id,
            account_id,
        )
        return LazyProvisionResult(outcome="not_applicable")

    kind = _CW_KIND_TO_POLARIS.get(inbox.get("channel_type", ""), "unknown")
    if kind not in _LAZY_PROVISIONABLE_KINDS:
        logger.info(
            "lazy_provision: kind %s (inbox=%s) out of lazy scope — skipping",
            kind,
            inbox_id,
        )
        return LazyProvisionResult(outcome="not_applicable", channel_kind=kind)

    # Entitlement gate — read allowed_channel_kinds for the owning tenant.
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT allowed_channel_kinds FROM tenant_quotas WHERE tenant_id = $1",
            tenant.tenant_id,
        )
    allowed = list(row["allowed_channel_kinds"] or []) if row else []
    if kind not in allowed:
        return LazyProvisionResult(outcome="disallowed", channel_kind=kind)

    connection_payload: dict[str, Any] = {"chatwoot_inbox_id": inbox_id}
    connection_payload.update(_extract_meta_ids(inbox))

    # Provision (+ audit) in one transaction on the service pool.
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await provision_channel(
                    conn,
                    tenant_id=tenant.tenant_id,
                    channel_kind=kind,
                    connection_payload=connection_payload,
                    bypass_tier_cap=False,  # entitlement already checked above
                )
                await audit_record(
                    conn,
                    tenant_id=tenant.tenant_id,
                    actor=None,  # system action — no human actor
                    action="channel.auto_provisioned",
                    after={
                        "channel_kind": kind,
                        "chatwoot_inbox_id": inbox_id,
                        "trigger": "first_inbound_message",
                    },
                )
        except asyncpg.UniqueViolationError:
            # A concurrent first message already provisioned this inbox — fine;
            # fall through to re-resolve below (idempotent).
            logger.info(
                "lazy_provision: inbox %s already provisioned (race) — re-resolving",
                inbox_id,
            )
        except TierCapError:
            # Raced with an entitlement narrowing between the check and provision.
            return LazyProvisionResult(outcome="disallowed", channel_kind=kind)
        except ValueError as exc:
            # Unreachable today (kind ∈ _LAZY_PROVISIONABLE_KINDS ⊂ provisionable,
            # inbox_id always present), but guard against a future refactor turning
            # this into a 500 → Chatwoot silent infinite-retry loop.
            logger.error(
                "lazy_provision: unexpected ValueError inbox=%s kind=%s: %s",
                inbox_id,
                kind,
                exc,
            )
            return LazyProvisionResult(outcome="not_applicable", channel_kind=kind)

    cw = get_chatwoot_client()

    # Best-effort token capture — never fails the provisioning.
    async with pool.acquire() as conn:
        try:
            token = await cw.get_channel_token(
                account_id=account_id, inbox_id=inbox_id
            )
            if token is not None:
                await secrets_svc.put(
                    conn,
                    tenant_id=tenant.tenant_id,
                    kind=_CHANNEL_TO_SECRET_KIND[kind],
                    plaintext=token.encode("utf-8"),
                )
        except Exception as exc:  # noqa: BLE001 — best-effort, never fail provisioning
            logger.warning(
                "lazy_provision: token capture failed inbox=%s kind=%s err=%s",
                inbox_id,
                kind,
                type(exc).__name__,
            )

    # Best-effort agent-bot attach — attach_inbox_agent_bot never raises on CW errors.
    try:
        await attach_inbox_agent_bot(cw, account_id=account_id, inbox_id=inbox_id)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "lazy_provision: agent-bot attach skipped inbox=%s err=%s",
            inbox_id,
            type(exc).__name__,
        )

    resolved, status_str, suspended_reason = await resolve_chatwoot_with_status(
        inbox_id
    )
    logger.info(
        "lazy_provision: provisioned tenant=%s inbox=%s kind=%s",
        tenant.tenant_id,
        inbox_id,
        kind,
    )
    return LazyProvisionResult(
        outcome="provisioned",
        resolved=resolved,
        tenant_status=status_str,
        suspended_reason=suspended_reason,
        channel_kind=kind,
    )
