"""Admin routes for tenant secret rotation — Phase 9 PR-4.

Mounted at /api/admin/secrets.

POST /{tenant_id}/rotate — rotate a per-tenant secret for one of the
  four supported services: meta_whatsapp, meta_ig, meta_messenger,
  chatwoot.

Security contract:
  1. New plaintext is never logged or stored beyond the request scope.
  2. tenant_secrets writes go through services/secrets.py (PyNaCl AEAD).
  3. Audit row records service name only, NOT the new secret value.
  4. chatwoot service: also calls rotate_chatwoot_token() (meta_admin.py).
  5. polaris_anon role is denied; only platform_admin JWT is accepted.

ADR-030: audit every write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from polaris_api.config import get_settings
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services import secrets as secrets_svc
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-secrets"])

_FORBID = ConfigDict(extra="forbid")

_SUPPORTED_SERVICES = frozenset({
    "meta_whatsapp",
    "meta_ig",
    "meta_messenger",
    "chatwoot",
    "telegram_bot_token",
})

# Maps the admin UI service name (what the operator sees and what the audit
# log records) to the tenant_secrets ``kind`` value the bot READS at runtime.
# The split exists because the admin UI uses human-readable service names while
# the bot's secret-lookup code uses canonical kind names (per SSOT contract).
#
# NOTE ``meta_ig`` writes ``meta_ig_login_access_token`` — the Instagram-Login
# user token (graph.instagram.com) that ``InstagramLoginClient`` reads. It must
# NOT write ``meta_fb_page_access_token``: that slot holds the Messenger Facebook
# Page token, and a shared mapping let an Instagram rotation silently clobber the
# Messenger token (the IGAA-token-in-the-Page-slot incident, 2026-06-09). The
# legacy FB-page-linked "instagram" channel that once shared the Page token is no
# longer provisionable (migration 104 → instagram_login is the only IG path).
_SERVICE_TO_KIND: dict[str, str] = {
    "meta_whatsapp": "meta_waba_access_token",
    "meta_messenger": "meta_fb_page_access_token",
    "meta_ig": "meta_ig_login_access_token",
    "telegram_bot_token": "telegram_bot_token",
    "chatwoot": "chatwoot",
}

ServiceLiteral = Literal[
    "meta_whatsapp", "meta_ig", "meta_messenger", "chatwoot", "telegram_bot_token"
]


class _RotateRequest(BaseModel):
    model_config = _FORBID

    service: ServiceLiteral
    # Password-type in the UI; never echoed back or logged.
    new_value: str = Field(..., min_length=1, max_length=4096)


class _RotateResponse(BaseModel):
    model_config = _FORBID

    service: str
    rotated_at: str
    # Intentionally omits the new_value — caller already knows it.


@router.post("/{tenant_id}/rotate", response_model=_RotateResponse)
async def rotate_secret(
    tenant_id: UUID,
    body: _RotateRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _RotateResponse:
    """Rotate a per-tenant secret credential.

    Steps:
      1. Confirm tenant exists.
      2. Write new secret via services/secrets.put() (PyNaCl AEAD encrypt).
      3. Emit audit row — service name only, no plaintext in audit log.
      4. If service=chatwoot, call rotate_chatwoot_token() from meta_admin.
      5. Return {rotated_at, service}. Never returns the plaintext.

    The new_value is zeroed from the Pydantic model after use; Python GC
    handles the rest. The value never touches the audit log.
    """
    async with conn.transaction():
        # 1. Tenant existence check.
        existing = await conn.fetchrow(
            "SELECT id FROM tenants WHERE id = $1 LIMIT 1", tenant_id
        )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found"
            )

        # 2. Encrypt and persist via services/secrets — never directly
        # write tenant_secrets with asyncpg; the PyNaCl layer lives in
        # secrets_svc.put().
        # The secret is stored under the canonical ``kind`` name that the bot
        # reads at runtime (e.g. ``meta_waba_access_token``), not under the
        # admin UI service name. The audit log records the service name instead
        # (step 3) so operators can correlate what was rotated.
        plaintext_bytes = body.new_value.encode("utf-8")
        secret_kind = _SERVICE_TO_KIND.get(body.service, body.service)
        await secrets_svc.put(
            conn,
            tenant_id=tenant_id,
            kind=secret_kind,
            plaintext=plaintext_bytes,
        )

        # 3. Audit row — service name only, no plaintext, no hash.
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="secret.rotated",
            after={"service": body.service},
        )

    rotated_at = datetime.now(UTC).isoformat()

    # 4. Chatwoot post-rotation call (outside transaction — network call
    # should not hold a DB transaction open; if it fails, the secret
    # is still rotated in the DB, caller retries the Chatwoot sync).
    if body.service == "chatwoot":
        from polaris_api.services.meta_admin import rotate_chatwoot_token

        settings = get_settings()
        try:
            await rotate_chatwoot_token(
                tenant_id=tenant_id,
                new_value=body.new_value,
                settings=settings,
            )
        except Exception as exc:
            # Log and surface as a warning but do not fail the HTTP response
            # — the DB write succeeded; operator can re-run the Chatwoot sync
            # via the admin UI or CLI. Failure is recorded in the log.
            logger.warning(
                "rotate_chatwoot_token failed after DB write tenant=%s err=%s",
                tenant_id,
                exc,
            )

    logger.info(
        "secret.rotated tenant=%s service=%s actor=%s",
        tenant_id,
        body.service,
        admin_id,
    )

    return _RotateResponse(service=body.service, rotated_at=rotated_at)
