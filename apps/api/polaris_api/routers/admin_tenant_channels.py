"""Admin routes — tenant channel add/remove + Telegram connect (Phase 10 PR-0-retrofit).

Mounted at ``/api/admin/tenants`` (same prefix as ``admin_tenants.py`` and
``admin_ea.py`` — mirrors the existing precedent of multiple routers sharing
one prefix). Extracted from ``admin_tenants.py`` in Batch E (task E4; see PRP
plan audit-batch-e-structural-splits) — route paths are unchanged.

Endpoints:

  * ``POST   /{tenant_id}/channels``          — add a channel with optional
                                                 tier-cap override
                                                 (platform_admin only;
                                                 impersonation sessions blocked)
  * ``DELETE /{tenant_id}/channels/{kind}``   — remove a channel
  * ``POST   /{tenant_id}/channels/telegram`` — connect a Telegram bot
                                                 (off-Meta channels M2)

Auth: ``require_platform_admin``, imported from ``admin_tenants.py`` — same
precedent ``admin_billing.py`` already uses.

ADR anchors:
  - ADR-015 Amendment 3rd — channel tier caps + override
  - ADR-030 — auth + audit hash chain
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from polaris_api.routers.admin_tenants import (
    _FORBID,
    _POLARIS_KIND_TO_SECRET,
    require_platform_admin,
)
from polaris_api.services import secrets as secrets_svc
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record
from polaris_api.services.channels.provision import (
    ChannelDeprovisionResult,
    ChannelProvisionResult,
    TierCapError,
    deprovision_channel,
    provision_channel,
)
from polaris_api.services.channels.telegram_provision import (
    TelegramProvisionResult,
    persist_telegram_channel,
    validate_telegram_bot,
)
from polaris_api.services.chatwoot.agent_bot import attach_inbox_agent_bot
from polaris_api.services.chatwoot.client import get_chatwoot_client
from polaris_api.services.telegram_client import TelegramError, get_telegram_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-tenants"])

_TOKEN_CHANNEL_KINDS: frozenset[str] = frozenset({"whatsapp", "messenger", "instagram_login"})


class _AdminChannelAddRequest(BaseModel):
    """Body for POST /api/admin/tenants/{tenant_id}/channels.

    Phase 10 PR-0-retrofit — ADR-015 Amendment 3rd.

    ``override_tier_cap=True`` bypasses BOTH the ``allowed_channel_kinds``
    membership check AND the Starter 1-inbox cap. Blank or missing
    ``override_reason`` when ``override_tier_cap=True`` is a 422 — the
    reason is mandatory so the quarterly P&L review query (../docs/internal-ops/RUNBOOK.md
    §"Channel override quarterly review") has an audit trail.

    ``connection_payload`` required key:
      - ``chatwoot_inbox_id`` (int) — Chatwoot inbox already created via
        the Chatwoot Platform API before calling this endpoint.

    Optional keys (channel-specific):
      - ``waba_id``, ``phone_number_id`` — WhatsApp
      - ``fb_page_id``                   — Messenger
      - ``ig_business_account_id``       — Instagram
      - ``wa_messaging_tier``            — WhatsApp messaging tier
    """

    model_config = _FORBID

    channel_kind: str
    connection_payload: dict[str, Any] = Field(default_factory=dict)
    override_tier_cap: bool = False
    override_reason: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.override_tier_cap and not (self.override_reason or "").strip():
            raise ValueError(
                "override_reason must be a non-empty string when override_tier_cap=True"
            )


class _ChannelAddResponse(BaseModel):
    model_config = _FORBID
    channel_id: UUID
    channel_kind: str
    override_applied: bool
    # True when the channel access token was successfully captured and stored in
    # tenant_secrets during this add operation. False when the channel doesn't
    # have auto-provision support (e.g. telegram), or when the token lookup
    # returned None (missing config, Chatwoot not reachable, etc.).
    token_captured: bool = False


class _ChannelRemoveResponse(BaseModel):
    model_config = _FORBID
    channel_kind: str
    # The tenant_secrets.kind that was deleted alongside the channel, or null
    # when nothing was removed (no secret, or a sibling channel — e.g. the
    # other of instagram/messenger — still uses the shared page token).
    secret_removed: str | None = None


class _AdminTelegramConnectRequest(BaseModel):
    """Body for POST /api/admin/tenants/{tenant_id}/channels/telegram.

    Off-Meta channels M2 (Chatwoot-native cutover). Sales-led: the admin pastes
    the tenant's bot token (from BotFather) + supplies the Chatwoot inbox id of a
    native ``Channel::Telegram`` inbox already created in Chatwoot (which owns
    the bot webhook), same shape as Meta. The endpoint validates the token
    (getMe) and provisions the row — it does NOT setWebhook (Chatwoot owns it).

    Telegram is gated to Pro+ via ``allowed_channel_kinds`` (same mechanism as
    IG/Messenger); ``override_tier_cap`` bypasses it with a mandatory reason.
    """

    model_config = _FORBID

    bot_token: str = Field(min_length=1)
    chatwoot_inbox_id: int
    override_tier_cap: bool = False
    override_reason: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.override_tier_cap and not (self.override_reason or "").strip():
            raise ValueError(
                "override_reason must be a non-empty string when override_tier_cap=True"
            )


class _TelegramConnectResponse(BaseModel):
    model_config = _FORBID
    channel_id: UUID
    bot_username: str | None
    override_applied: bool


@router.post(
    "/{tenant_id}/channels",
    response_model=_ChannelAddResponse,
    status_code=201,
)
async def add_channel(
    tenant_id: UUID,
    body: _AdminChannelAddRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
    request: Request,
) -> _ChannelAddResponse:
    """Add a channel to a tenant — with optional tier-cap override.

    Phase 10 PR-0-retrofit — ADR-015 Amendment 3rd.

    Security invariants enforced here (not just Pydantic):
      1. ``X-Polaris-Impersonating`` header present → 403. The override
         checkbox must never be reachable during an impersonation session.
      2. ``override_tier_cap=True`` with blank reason → 422 (caught by
         ``_AdminChannelAddRequest.model_post_init`` before reaching here).
      3. Audit row with ``action='channels_per_kind.override'`` is ALWAYS
         written inside the same transaction as the INSERT so the override
         is never silent.
    """
    # Security invariant #1: block impersonation sessions.
    # The impersonation start endpoint sets X-Polaris-Impersonating on
    # subsequent requests within the session; no platform_admin should be
    # adding channels while operating as a tenant user.
    if request.headers.get("x-polaris-impersonating"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden_during_impersonation",
        )

    # Verify tenant exists before acquiring transaction lock.
    tenant_row = await conn.fetchrow(
        "SELECT id, status, chatwoot_account_id FROM tenants WHERE id = $1", tenant_id
    )
    if tenant_row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    if tenant_row["status"] == "deleted":
        raise HTTPException(status_code=409, detail="tenant_deleted")

    cw_account_id: int | None = tenant_row["chatwoot_account_id"]

    # Snapshot "before" state for the audit row.
    before_channel_count: int = (
        await conn.fetchval(
            "SELECT COUNT(*) FROM tenant_channels WHERE tenant_id = $1",
            tenant_id,
        )
        or 0
    )
    plan_tier: str | None = await conn.fetchval(
        "SELECT plan_tier FROM tenant_quotas WHERE tenant_id = $1",
        tenant_id,
    )

    async with conn.transaction():
        try:
            result: ChannelProvisionResult = await provision_channel(
                conn,
                tenant_id=tenant_id,
                channel_kind=body.channel_kind,
                connection_payload=body.connection_payload,
                bypass_tier_cap=body.override_tier_cap,
            )
        except TierCapError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="chatwoot_inbox_id_already_registered",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        # Security invariant #3: always emit an audit row; action differs
        # based on whether a bypass was used.
        audit_action = "channels_per_kind.override" if body.override_tier_cap else "channel.added"
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action=audit_action,
            before={
                "plan_tier": plan_tier,
                "channel_count": before_channel_count,
            },
            after={
                "channel_kind": body.channel_kind,
                "channel_id": str(result.channel_id),
                "channel_count": before_channel_count + 1,
                "override_tier_cap": body.override_tier_cap,
                "override_reason": body.override_reason,
            },
        )

    # ---- Best-effort channel token auto-provisioning ----
    # For Meta channels (whatsapp, instagram, messenger) we attempt to fetch
    # the channel access token from the Chatwoot internal endpoint and store
    # it in tenant_secrets so the bot can dispatch outbound messages without
    # the admin needing to manually rotate the secret. This is BEST-EFFORT:
    # a failure does NOT fail the channel-add. The admin can always rotate
    # the token manually via the secrets page.
    token_captured = False
    if body.channel_kind in _TOKEN_CHANNEL_KINDS:
        chatwoot_inbox_id_for_token = body.connection_payload.get("chatwoot_inbox_id")
        cw_account_for_token = cw_account_id
        if chatwoot_inbox_id_for_token is not None and cw_account_for_token is not None:
            try:
                inbox_id_int = int(chatwoot_inbox_id_for_token)
                channel_token = await get_chatwoot_client().get_channel_token(
                    account_id=cw_account_for_token,
                    inbox_id=inbox_id_int,
                )
                if channel_token is not None:
                    secret_kind = _POLARIS_KIND_TO_SECRET[body.channel_kind]
                    await secrets_svc.put(
                        conn,
                        tenant_id=tenant_id,
                        kind=secret_kind,
                        plaintext=channel_token.encode("utf-8"),
                    )
                    token_captured = True
                    logger.info(
                        "add_channel: token auto-provisioned tenant=%s channel=%s kind=%s",
                        tenant_id,
                        body.channel_kind,
                        secret_kind,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "add_channel: token auto-provision failed tenant=%s channel=%s err=%s",
                    tenant_id,
                    body.channel_kind,
                    type(exc).__name__,
                )

    # ---- Best-effort agent-bot auto-attach ----
    # Link the shared platform 'Polaris Bot' to the new inbox so conversations
    # start in `pending` (bot-owned) — the human-handling gate + native
    # escalation depend on this. Intentionally unconditional on channel_kind
    # (unlike token capture above, which is Meta-only): EVERY inbox needs the
    # bot. Post-commit + best-effort: a Chatwoot outage
    # must not fail the channel add (scripts/backfill_agent_bot_attach.py
    # reconciles any inbox left unattached). attach_inbox_agent_bot never
    # raises on Chatwoot errors; the guard here is only for a bad inbox id.
    cw_inbox_id_for_bot = body.connection_payload.get("chatwoot_inbox_id")
    if cw_account_id is not None and cw_inbox_id_for_bot is not None:
        try:
            await attach_inbox_agent_bot(
                get_chatwoot_client(),
                account_id=cw_account_id,
                inbox_id=int(cw_inbox_id_for_bot),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "add_channel: agent-bot attach skipped (bad inbox id) tenant=%s err=%s",
                tenant_id,
                type(exc).__name__,
            )

    return _ChannelAddResponse(
        channel_id=result.channel_id,
        channel_kind=result.channel_kind,
        override_applied=body.override_tier_cap,
        token_captured=token_captured,
    )


@router.delete(
    "/{tenant_id}/channels/{channel_kind}",
    response_model=_ChannelRemoveResponse,
    status_code=200,
)
async def remove_channel(
    tenant_id: UUID,
    channel_kind: str,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
    request: Request,
) -> _ChannelRemoveResponse:
    """Remove a single channel from a tenant (the inverse of ``add_channel``).

    Deletes the ``tenant_channels`` row and its captured outbound-token secret
    (only when no sibling channel still shares that token kind), inside one
    transaction with an ``action='channel.removed'`` audit row. Lets an admin
    reconfigure one channel without deleting and rebuilding the whole tenant.

    Does NOT touch the Chatwoot inbox or the WABA/Meta webhook registration —
    those are managed in Chatwoot/Meta, not here.

    Security: blocked during impersonation (same invariant as ``add_channel``).
    """
    if request.headers.get("x-polaris-impersonating"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden_during_impersonation",
        )

    tenant_row = await conn.fetchrow("SELECT id FROM tenants WHERE id = $1", tenant_id)
    if tenant_row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    async with conn.transaction():
        try:
            result: ChannelDeprovisionResult = await deprovision_channel(
                conn,
                tenant_id=tenant_id,
                channel_kind=channel_kind,
            )
        except ValueError as exc:
            # Not provisionable kind, or no such channel row for this tenant.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="channel.removed",
            before={"channel_kind": channel_kind},
            after={"secret_removed": result.secret_kind_removed},
        )

    return _ChannelRemoveResponse(
        channel_kind=result.channel_kind,
        secret_removed=result.secret_kind_removed,
    )


@router.post(
    "/{tenant_id}/channels/telegram",
    response_model=_TelegramConnectResponse,
    status_code=201,
)
async def connect_telegram(
    tenant_id: UUID,
    body: _AdminTelegramConnectRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
    request: Request,
) -> _TelegramConnectResponse:
    """Connect a Telegram bot to a tenant — off-Meta channels M2
    (Chatwoot-native cutover).

    Validates the bot token (getMe) and provisions the ``tenant_channels`` row +
    encrypted token. Same security invariants as ``add_channel`` (no
    impersonation; mandatory override reason; always-audited).

    Chatwoot owns the Telegram bot webhook now (a native ``Channel::Telegram``
    inbox the admin creates in Chatwoot, whose id is passed as
    ``chatwoot_inbox_id``). Polaris therefore NO LONGER calls ``setWebhook`` —
    Telegram allows one webhook URL per bot, so pointing it at Polaris would
    override Chatwoot's and break native inbound. Inbound arrives via the
    agent-bot path (``resolve_chatwoot`` → channel_kind 'telegram'); Polaris
    keeps the bot token only to send bot replies DIRECT + mirror them into
    Chatwoot, exactly like the Meta surfaces.
    """
    if request.headers.get("x-polaris-impersonating"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden_during_impersonation",
        )

    tenant_row = await conn.fetchrow("SELECT id, status FROM tenants WHERE id = $1", tenant_id)
    if tenant_row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    if tenant_row["status"] == "deleted":
        raise HTTPException(status_code=409, detail="tenant_deleted")

    before_channel_count: int = (
        await conn.fetchval("SELECT COUNT(*) FROM tenant_channels WHERE tenant_id = $1", tenant_id)
        or 0
    )
    plan_tier: str | None = await conn.fetchval(
        "SELECT plan_tier FROM tenant_quotas WHERE tenant_id = $1", tenant_id
    )

    telegram_client = get_telegram_client()

    # NETWORK phase BEFORE the transaction — getMe can take ~20s; holding a
    # pooled connection / open tx across that is an anti-pattern (same lesson as
    # create_tenant's SMTP round-trip). Nothing is persisted yet, so a bad token
    # / unreachable Telegram aborts cleanly with no DB writes. Chatwoot-native
    # cutover: NO setWebhook here — Chatwoot owns the bot webhook.
    try:
        validation = await validate_telegram_bot(telegram_client, bot_token=body.bot_token)
    except TelegramError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"telegram_validation_failed: HTTP {exc.status_code}",
        ) from exc

    async with conn.transaction():
        try:
            result: TelegramProvisionResult = await persist_telegram_channel(
                conn,
                tenant_id=tenant_id,
                bot_token=body.bot_token,
                validation=validation,
                chatwoot_inbox_id=body.chatwoot_inbox_id,
                bypass_tier_cap=body.override_tier_cap,
            )
        except TierCapError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="chatwoot_inbox_id_already_registered",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        audit_action = "channels_per_kind.override" if body.override_tier_cap else "channel.added"
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action=audit_action,
            before={"plan_tier": plan_tier, "channel_count": before_channel_count},
            after={
                "channel_kind": "telegram",
                "channel_id": str(result.channel_id),
                "bot_id": result.bot_id,
                "bot_username": result.bot_username,
                "channel_count": before_channel_count + 1,
                "override_tier_cap": body.override_tier_cap,
                "override_reason": body.override_reason,
            },
        )

    return _TelegramConnectResponse(
        channel_id=result.channel_id,
        bot_username=result.bot_username,
        override_applied=body.override_tier_cap,
    )
