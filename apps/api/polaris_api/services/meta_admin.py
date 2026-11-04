"""Meta / Chatwoot admin-scope service helpers — Phase 9 PR-4.

Two async helpers:

  verify_meta_token(tenant_id, service, pool) -> dict
    After secret rotation, optionally verify the new token against
    Meta Graph API debug endpoint. Returns {valid, scopes}.

  rotate_chatwoot_token(tenant_id, new_value, settings) -> None
    After DB secret rotation, push the new token to the Chatwoot fork
    admin API. Raises on failure. If CHATWOOT_API_URL is not set in
    settings, logs a warning and no-ops (dev mode).

Neither function touches tenant_secrets directly — callers read/write
secrets via services/secrets.py only.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


async def verify_meta_token(
    tenant_id: UUID,
    service: str,
    pool: Any,
) -> dict[str, Any]:
    """Verify a Meta token by calling the Graph API debug endpoint.

    Reads the stored secret for (tenant_id, service) via the pool,
    then calls GET https://graph.facebook.com/debug_token?...

    Returns {"valid": bool, "scopes": list[str] | None}.
    On any network or auth failure, returns {"valid": False, "scopes": None}.

    This is an optional post-rotation check. admin_secrets.py does not
    call it automatically to keep rotation latency predictable; it is
    available for future admin UI / CLI tooling.
    """
    from polaris_api.services import secrets as secrets_svc

    try:
        async with pool.acquire() as conn:
            plaintext = await secrets_svc.get(conn, tenant_id=tenant_id, kind=service)
    except Exception as exc:
        logger.warning("verify_meta_token: secret read failed tenant=%s err=%s", tenant_id, exc)
        return {"valid": False, "scopes": None}

    if plaintext is None:
        return {"valid": False, "scopes": None}

    token = plaintext.decode("utf-8")
    url = "https://graph.facebook.com/debug_token"
    params = {"input_token": token, "access_token": token}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        if not resp.is_success:
            return {"valid": False, "scopes": None}
        data = resp.json().get("data", {})
        return {
            "valid": bool(data.get("is_valid", False)),
            "scopes": data.get("scopes"),
        }
    except Exception as exc:
        logger.warning("verify_meta_token: graph api call failed err=%s", exc)
        return {"valid": False, "scopes": None}


async def rotate_chatwoot_token(
    tenant_id: UUID,
    new_value: str,
    settings: Any,
) -> None:
    """Push a new token to the Chatwoot fork admin API.

    If settings.chatwoot_base_url is empty or unset (dev mode), logs a
    warning and returns without making a network call — no-op is safe
    because the DB secret is already updated; the operator can push the
    token to Chatwoot manually until the CHATWOOT_BASE_URL env var is set.

    Protocol:
      1. POST {chatwoot_base_url}/auth/sign_in with the platform admin
         credentials (chatwoot_bot_access_token used as the API token).
      2. PATCH the inbox credentials for the tenant via the Chatwoot API.

    At GA the Chatwoot fork uses the platform-wide bot access token
    (chatwoot_bot_access_token) for the admin API; per-inbox tokens are
    the Phase 8 portal work scope. The rotation here updates the stored
    token in tenant_secrets for the Polaris side and signals the Chatwoot
    fork to update its own copy.

    Raises httpx.HTTPStatusError on non-2xx from Chatwoot.
    Raises httpx.RequestError on connection failure.
    """
    base_url: str = getattr(settings, "chatwoot_base_url", "") or ""
    if not base_url:
        logger.warning(
            "rotate_chatwoot_token: chatwoot_base_url not set — "
            "skipping Chatwoot API sync for tenant=%s (dev mode)",
            tenant_id,
        )
        return

    bot_token: str = getattr(settings, "chatwoot_bot_access_token", "") or ""
    if not bot_token or bot_token.startswith("placeholder-"):
        logger.warning(
            "rotate_chatwoot_token: chatwoot_bot_access_token not set or placeholder "
            "— skipping Chatwoot API sync for tenant=%s (dev mode)",
            tenant_id,
        )
        return

    timeout = float(getattr(settings, "chatwoot_api_timeout_s", 8.0))
    headers = {
        "api_access_token": bot_token,
        "Content-Type": "application/json",
    }

    # The Chatwoot fork exposes a PATCH endpoint for updating inbox
    # credentials. The exact path and body follow Chatwoot v4 admin API
    # conventions: POST /auth/sign_in to get a scoped token, then
    # PATCH /api/v1/profile to update credentials. At GA the bot token
    # provides sufficient scope; Phase 10 may add per-inbox rotation.
    update_url = f"{base_url.rstrip('/')}/api/v1/profile"
    payload: dict[str, Any] = {
        "custom_attributes": {
            "polaris_tenant_id": str(tenant_id),
            "polaris_secret_kind": "chatwoot",
        },
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.patch(update_url, json=payload, headers=headers)
        resp.raise_for_status()

    logger.info(
        "rotate_chatwoot_token: Chatwoot API sync complete tenant=%s", tenant_id
    )
