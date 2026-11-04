"""Admin routes — tenant email inbox info + IMAP/SMTP credential rotation.

Mounted at ``/api/admin/tenants`` (same prefix as ``admin_tenants.py`` and
``admin_ea.py``). Extracted from ``admin_tenants.py`` in Batch E (task E4;
see PRP plan audit-batch-e-structural-splits) — route paths are unchanged.

Endpoints:

  * ``GET   /{tenant_id}/channels/inbox-info``               — normalised
                                                                 Chatwoot inbox
                                                                 metadata for the
                                                                 admin channel-add
                                                                 modal
  * ``GET   /{tenant_id}/channels/email-config``              — IMAP/SMTP config
                                                                 for all email
                                                                 inboxes
  * ``PATCH /{tenant_id}/channels/email-credentials/{inbox_id}`` — rotate an
                                                                     IMAP/SMTP
                                                                     password via
                                                                     Chatwoot

Email credentials are Chatwoot-native (IMAP/SMTP live in Chatwoot, not in
tenant_secrets) — these endpoints read/write Chatwoot directly.

Auth: ``require_platform_admin``, imported from ``admin_tenants.py`` — same
precedent ``admin_billing.py`` already uses. ``_mask_last4`` is also imported
from there (shared with ``get_tenant``'s secret preview).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from polaris_api.routers.admin_tenants import (
    _FORBID,
    _POLARIS_KIND_TO_SECRET,
    _mask_last4,
    require_platform_admin,
)
from polaris_api.services import secrets as secrets_svc
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record
from polaris_api.services.chatwoot.client import ChatwootError, get_chatwoot_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-tenants"])

_CW_KIND_TO_POLARIS: dict[str, str] = {
    "Channel::Whatsapp": "whatsapp",
    # Chatwoot's "Continue with Instagram" creates a Channel::Instagram inbox
    # using the Instagram API with Instagram Login (graph.instagram.com) — the
    # native/modern path. It maps to Polaris 'instagram_login', NOT the legacy
    # FB-page 'instagram' (Messenger-API-for-Instagram), which has no native
    # Chatwoot counterpart and stays manual-select only.
    "Channel::Instagram": "instagram_login",
    "Channel::FacebookPage": "messenger",
    "Channel::Telegram": "telegram",
    "Channel::Email": "email",
}


class _InboxInfoResponse(BaseModel):
    inbox_name: str | None = None
    channel_type: str
    waba_id: str | None = None
    phone_number_id: str | None = None
    ig_business_account_id: str | None = None
    fb_page_id: str | None = None
    telegram_bot_name: str | None = None
    error: str | None = None
    # True when a tenant_secrets row already exists for the canonical secret
    # kind expected by this channel. Does NOT return the secret value itself.
    token_captured: bool = False
    # Masked last-4 preview of the channel's access token, e.g. "••••3a2f".
    # None if the token is not retrievable. NEVER contains more than 4 raw chars.
    token_preview: str | None = None


@router.get("/{tenant_id}/channels/inbox-info", response_model=_InboxInfoResponse)
async def get_inbox_info(
    tenant_id: UUID,
    chatwoot_inbox_id: Annotated[int, Query(ge=1)],
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _InboxInfoResponse:
    """Return normalised Chatwoot inbox metadata for the admin channel-add modal.

    Always 200 — sets ``error`` on Chatwoot lookup failure so the frontend
    can show a soft warning without blocking form submission.

    Uses ``list_inboxes`` (GET /inboxes) rather than the single-inbox show
    endpoint (GET /inboxes/:id) because the show endpoint requires the
    Chatwoot user to have administrator role on the account — a regular
    agent token gets 403.  The list endpoint works at agent level and is
    already account-scoped, so the cross-account guard is implicit.
    """
    tenant_row = await conn.fetchrow(
        "SELECT chatwoot_account_id FROM tenants WHERE id = $1", tenant_id
    )
    if tenant_row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    cw_account_id: int | None = tenant_row["chatwoot_account_id"]
    if not cw_account_id:
        return _InboxInfoResponse(
            channel_type="unknown",
            error="tenant_no_chatwoot_account",
        )

    try:
        all_inboxes = await get_chatwoot_client().list_inboxes(account_id=cw_account_id)
    except ChatwootError as exc:
        logger.warning(
            "inbox_info list_inboxes failed: tenant=%s inbox=%d err=%s",
            tenant_id,
            chatwoot_inbox_id,
            exc,
        )
        return _InboxInfoResponse(channel_type="unknown", error=str(exc))

    inbox: dict[str, Any] | None = next(
        (i for i in all_inboxes if i.get("id") == chatwoot_inbox_id), None
    )
    if inbox is None:
        return _InboxInfoResponse(channel_type="unknown", error="inbox_not_found")

    raw_kind = inbox.get("channel_type", "")
    polaris_kind = _CW_KIND_TO_POLARIS.get(raw_kind, "unknown")

    # Chatwoot stores WA Cloud API IDs in provider_config for API-provisioned
    # inboxes, but in additional_attributes for dashboard-created ones.
    # Check both and use whichever is populated.
    provider_config: dict[str, Any] = inbox.get("provider_config") or {}
    additional_attrs: dict[str, Any] = inbox.get("additional_attributes") or {}

    def _wa(key: str) -> str | None:
        val = provider_config.get(key) or additional_attrs.get(key)
        return str(val) if val else None

    # Check if the canonical secret kind for this channel already has a row
    # in tenant_secrets. Returns True so the admin modal can show a badge
    # confirming the token is present. The secret value itself is never returned.
    token_captured = False
    expected_kind = _POLARIS_KIND_TO_SECRET.get(polaris_kind)
    if expected_kind is not None:
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM tenant_secrets WHERE tenant_id = $1 AND kind = $2 LIMIT 1",
                tenant_id,
                expected_kind,
            )
            token_captured = exists is not None
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — log and leave token_captured=False.
            logger.warning(
                "inbox_info: token_captured check failed tenant=%s kind=%s err=%s",
                tenant_id,
                expected_kind,
                exc,
            )

    # Compute a masked last-4 preview of the channel token, best-effort.
    # WhatsApp: use api_key from the Chatwoot inbox payload (Chatwoot exposes it
    #   on the list endpoint for WhatsApp channels).
    # Instagram / Messenger: read the stored Polaris secret (Chatwoot redacts
    #   the page token in its API response).
    # Telegram: read the stored telegram_bot_token secret.
    # Any failure → token_preview=None (never raises, never logs the token).
    token_preview: str | None = None
    try:
        if polaris_kind == "whatsapp":
            raw_api_key = inbox.get("api_key") or None
            if raw_api_key and isinstance(raw_api_key, str):
                token_preview = _mask_last4(raw_api_key)
        elif polaris_kind == "messenger":
            secret_bytes = await secrets_svc.get(conn, tenant_id, "meta_fb_page_access_token")
            if secret_bytes:
                token_preview = _mask_last4(secret_bytes.decode())
        elif polaris_kind == "instagram_login":
            secret_bytes = await secrets_svc.get(conn, tenant_id, "meta_ig_login_access_token")
            if secret_bytes:
                token_preview = _mask_last4(secret_bytes.decode())
        elif polaris_kind == "telegram":
            secret_bytes = await secrets_svc.get(conn, tenant_id, "telegram_bot_token")
            if secret_bytes:
                token_preview = _mask_last4(secret_bytes.decode())
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "inbox_info: token_preview computation failed tenant=%s kind=%s err_type=%s",
            tenant_id,
            polaris_kind,
            type(exc).__name__,
        )
        token_preview = None

    return _InboxInfoResponse(
        inbox_name=inbox.get("name") or None,
        channel_type=polaris_kind,
        waba_id=_wa("business_account_id"),
        phone_number_id=_wa("phone_number_id"),
        ig_business_account_id=str(inbox["instagram_id"]) if inbox.get("instagram_id") else None,
        fb_page_id=str(inbox["page_id"]) if inbox.get("page_id") else None,
        telegram_bot_name=inbox.get("bot_name") or None,
        token_captured=token_captured,
        token_preview=token_preview,
    )


class _EmailInboxConfig(BaseModel):
    inbox_id: int
    inbox_name: str | None = None
    email: str | None = None
    imap_enabled: bool = False
    imap_address: str | None = None
    imap_port: int | None = None
    imap_login: str | None = None
    imap_enable_ssl: bool = False
    imap_authentication: str | None = None
    smtp_enabled: bool = False
    smtp_address: str | None = None
    smtp_port: int | None = None
    smtp_domain: str | None = None
    smtp_login: str | None = None
    smtp_enable_ssl_tls: bool = False
    smtp_enable_starttls_auto: bool = False
    smtp_openssl_verify_mode: str | None = None
    smtp_authentication: str | None = None


class _EmailConfigListResponse(BaseModel):
    inboxes: list[_EmailInboxConfig] = []
    error: str | None = None


class _EmailCredentialUpdateRequest(BaseModel):
    model_config = _FORBID

    field: Literal["imap_password", "smtp_password"]
    new_value: str = Field(..., min_length=1, max_length=4096)


class _EmailCredentialUpdateResponse(BaseModel):
    inbox_id: int
    field: str
    updated_at: str


def _safe_port(raw: dict[str, Any], key: str) -> int | None:
    """Parse an optional port integer from a Chatwoot inbox dict.

    Uses an ``is not None`` guard (not a falsy check) so that a port value of
    ``0`` is preserved rather than coerced to ``None``. Wraps ``int()`` so that
    a non-numeric string (e.g. ``"auto"``) from Chatwoot raises ``ValueError``
    with a clear key name rather than propagating as a cryptic ``KeyError``.
    """
    val = raw.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{key!r} is not a valid port integer: {val!r}") from exc


def _parse_email_inbox(raw: dict[str, Any]) -> _EmailInboxConfig:
    """Extract email-relevant fields from a raw Chatwoot inbox dict.

    Raises ``KeyError`` if ``id`` is absent, or ``ValueError`` via
    ``_safe_port`` if a port field is non-numeric. Callers must wrap this
    function in a try/except that returns a soft error rather than a 500.
    """
    return _EmailInboxConfig(
        inbox_id=raw["id"],
        inbox_name=raw.get("name") or None,
        email=raw.get("email") or None,
        imap_enabled=bool(raw.get("imap_enabled")),
        imap_address=raw.get("imap_address") or None,
        imap_port=_safe_port(raw, "imap_port"),
        imap_login=raw.get("imap_login") or None,
        imap_enable_ssl=bool(raw.get("imap_enable_ssl")),
        imap_authentication=raw.get("imap_authentication") or None,
        smtp_enabled=bool(raw.get("smtp_enabled")),
        smtp_address=raw.get("smtp_address") or None,
        smtp_port=_safe_port(raw, "smtp_port"),
        smtp_domain=raw.get("smtp_domain") or None,
        smtp_login=raw.get("smtp_login") or None,
        smtp_enable_ssl_tls=bool(raw.get("smtp_enable_ssl_tls")),
        smtp_enable_starttls_auto=bool(raw.get("smtp_enable_starttls_auto")),
        smtp_openssl_verify_mode=raw.get("smtp_openssl_verify_mode") or None,
        smtp_authentication=raw.get("smtp_authentication") or None,
    )


@router.get(
    "/{tenant_id}/channels/email-config",
    response_model=_EmailConfigListResponse,
)
async def get_email_config(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _EmailConfigListResponse:
    """Return IMAP/SMTP config for all email inboxes in the tenant's Chatwoot account.

    Email credentials are Chatwoot-native (IMAP/SMTP live in Chatwoot, not in
    tenant_secrets), so this reads config straight from Chatwoot — it lists all
    inboxes and filters for Channel::Email rather than going through Polaris's
    tenant_channels rows. (The routing binding itself DOES live in a
    tenant_channels row; this endpoint just doesn't need it.) Always 200; sets
    ``error`` on Chatwoot failure.
    """
    tenant_row = await conn.fetchrow(
        "SELECT chatwoot_account_id FROM tenants WHERE id = $1", tenant_id
    )
    if tenant_row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    cw_account_id: int | None = tenant_row["chatwoot_account_id"]
    if not cw_account_id:
        return _EmailConfigListResponse(error="tenant_no_chatwoot_account")

    try:
        all_inboxes = await get_chatwoot_client().list_inboxes(account_id=cw_account_id)
    except ChatwootError as exc:
        logger.warning("email_config list_inboxes failed: tenant=%s err=%s", tenant_id, exc)
        return _EmailConfigListResponse(error=str(exc))

    try:
        email_inboxes = [
            _parse_email_inbox(inbox)
            for inbox in all_inboxes
            if inbox.get("channel_type") == "Channel::Email"
        ]
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("email_config parse failed: tenant=%s err=%s", tenant_id, exc)
        return _EmailConfigListResponse(error=f"inbox_parse_error: {exc}")

    return _EmailConfigListResponse(inboxes=email_inboxes)


@router.patch(
    "/{tenant_id}/channels/email-credentials/{inbox_id}",
    response_model=_EmailCredentialUpdateResponse,
)
async def update_email_credentials(
    tenant_id: UUID,
    inbox_id: int,
    body: _EmailCredentialUpdateRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _EmailCredentialUpdateResponse:
    """Update IMAP or SMTP password for an email inbox via Chatwoot.

    Credentials live in Chatwoot (not Polaris tenant_secrets), so this
    PATCHes the Chatwoot inbox directly. The new password is never stored
    or logged in Polaris. Emits an audit row with field name only.
    """
    tenant_row = await conn.fetchrow(
        "SELECT chatwoot_account_id FROM tenants WHERE id = $1", tenant_id
    )
    if tenant_row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    cw_account_id: int | None = tenant_row["chatwoot_account_id"]
    if not cw_account_id:
        raise HTTPException(status_code=409, detail="tenant_no_chatwoot_account")

    # Verify ownership and channel type before writing.
    # Super-admin tokens can cross account boundaries on shared Chatwoot
    # instances, so Polaris enforces the scope check explicitly.
    try:
        target_inbox = await get_chatwoot_client().get_inbox(
            account_id=cw_account_id,
            inbox_id=inbox_id,
        )
    except ChatwootError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="inbox_not_found",
        ) from exc

    if target_inbox.get("account_id") is not None and target_inbox["account_id"] != cw_account_id:
        raise HTTPException(status_code=403, detail="inbox_not_in_account")

    if target_inbox.get("channel_type") != "Channel::Email":
        raise HTTPException(status_code=409, detail="inbox_not_email_channel")

    try:
        await get_chatwoot_client().update_inbox_channel(
            account_id=cw_account_id,
            inbox_id=inbox_id,
            channel_attrs={body.field: body.new_value},
        )
    except ChatwootError as exc:
        logger.warning(
            "email_credentials update failed: tenant=%s inbox=%d field=%s err=%s",
            tenant_id,
            inbox_id,
            body.field,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"chatwoot_update_failed: {exc}",
        ) from exc

    rotated_at = datetime.now(UTC).isoformat()

    # The Chatwoot credential rotation already succeeded at this point.
    # Audit failure must not surface as a 500 — log and continue.
    try:
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="secret.rotated",
            after={"service": f"email_{body.field}", "inbox_id": inbox_id},
        )
    except Exception:
        logger.exception(
            "email_credentials audit_record failed (rotation succeeded): "
            "tenant=%s inbox=%d field=%s",
            tenant_id,
            inbox_id,
            body.field,
        )

    return _EmailCredentialUpdateResponse(
        inbox_id=inbox_id,
        field=body.field,
        updated_at=rotated_at,
    )
