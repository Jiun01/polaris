"""Admin impersonation endpoints -- Phase 9 PR-5.

Mounted at /api/admin.

Endpoints:
  POST   /impersonate/{tenant_id}/{user_id}  -- start impersonation session
  DELETE /impersonate                         -- stop impersonation session

Auth: require_platform_admin.

SECURITY INVARIANTS:
  1. Cookie is HttpOnly -- JS cannot read it.
  2. Cookie is signed with jwt_secret (HS256).
  3. The cookie is informational-only; it NEVER elevates portal privileges.
  4. Audit rows carry both actor_id=platform_admin AND impersonating_id=user.
  5. stop_impersonation writes an audit row even if the cookie is expired.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from polaris_api.config import get_settings
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin.pool import get_admin_service_conn
from polaris_api.services.impersonation import (
    COOKIE_DOMAIN,
    COOKIE_MAX_AGE_S,
    COOKIE_NAME,
    start_impersonation,
    stop_impersonation,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-impersonation"])

_FORBID = ConfigDict(extra="forbid")


class _StartResponse(BaseModel):
    model_config = _FORBID
    session_id: UUID
    expires_at: str


def _is_secure() -> bool:
    return get_settings().environment != "local"


@router.post("/impersonate/{tenant_id}/{user_id}", response_model=_StartResponse)
async def start_impersonation_endpoint(
    tenant_id: UUID,
    user_id: UUID,
    response: Response,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _StartResponse:
    """Start an impersonation session.

    Sets pol_impersonation cookie on .getpolaris.my.
    Writes an audit row under the target tenant chain.
    """
    # Verify tenant and user exist before starting session.
    from polaris_api.db.pools import get_service_pool
    pool = get_service_pool()

    # Check tenant exists.
    row = await conn.fetchrow(
        "SELECT id FROM tenants WHERE id = $1 AND status != 'deleted'", tenant_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    # Check user exists and belongs to tenant.
    # We query tenant_users only (public schema, accessible to polaris_service).
    # Joining auth.users would require USAGE on the auth schema which
    # polaris_service intentionally lacks (see migration 062 for context).
    user_row = await conn.fetchrow(
        "SELECT user_id FROM tenant_users WHERE tenant_id = $1 AND user_id = $2",
        tenant_id, user_id,
    )
    if user_row is None:
        raise HTTPException(status_code=404, detail="user_not_found_in_tenant")

    cookie_value, session_id, expires_at = await start_impersonation(
        platform_admin_id=admin_id,
        target_tenant_id=tenant_id,
        target_user_id=user_id,
        pool=pool,
    )

    # Set the signed HttpOnly cookie.
    # Domain:  .getpolaris.my in prod/staging (broad subdomain sharing).
    #          None in local dev — Chrome headless rejects Domain=.localhost
    #          cookies entirely (they don't appear in the cookie jar), which
    #          breaks the E2E impersonation test.  With domain=None the cookie
    #          is a host-only cookie for admin.localhost; the E2E test then
    #          reads it via context.cookies() and re-plants it for portal.localhost.
    secure = _is_secure()
    domain: str | None = COOKIE_DOMAIN if secure else None
    response.set_cookie(
        key=COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=COOKIE_MAX_AGE_S,
        path="/",
        domain=domain,
    )

    return _StartResponse(
        session_id=session_id,
        expires_at=expires_at.astimezone(UTC).isoformat(),
    )


@router.delete("/impersonate", status_code=204)
async def stop_impersonation_endpoint(
    request: Request,
    response: Response,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> None:
    """Stop an impersonation session.

    Clears the pol_impersonation cookie.
    Writes an audit row even if the cookie has expired.
    """
    cookie_value = request.cookies.get(COOKIE_NAME)
    _secure = _is_secure()
    _local_domain = ".localhost"
    if not cookie_value:
        # No session active -- clear cookie anyway and return.
        if _secure:
            response.delete_cookie(COOKIE_NAME, path="/", domain=COOKIE_DOMAIN)
        else:
            response.delete_cookie(COOKIE_NAME, path="/", domain=_local_domain)
        return

    from polaris_api.db.pools import get_service_pool
    pool = get_service_pool()

    try:
        await stop_impersonation(cookie_value, pool)
    except ValueError as exc:
        # Invalid cookie signature -- still clear the cookie.
        logger.warning("stop_impersonation: invalid cookie: %s", exc)
    finally:
        if _secure:
            response.delete_cookie(COOKIE_NAME, path="/", domain=COOKIE_DOMAIN)
        else:
            response.delete_cookie(COOKIE_NAME, path="/", domain=_local_domain)
