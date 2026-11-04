"""Impersonation cookie service -- Phase 9 PR-5.

Platform admins can start a session that views a tenant's portal as that
tenant's user. A signed cookie is set on .getpolaris.my so the portal app
can show a persistent "admin is watching" banner without any privilege
escalation.

SECURITY INVARIANTS (enforced here and reviewed in the pre-PR security review):
  1. The cookie is HttpOnly -- JS in the portal cannot read or forge it.
  2. Signing uses HS256 (PyJWT) with settings.jwt_secret -- same key the API
     already uses for portal tokens; verified on the portal side.
  3. impersonation NEVER elevates portal privileges. The portal reads the
     cookie ONLY to show the banner (apps/portal/components/shell/
     ImpersonationBanner.tsx). Middleware surfaces isImpersonating in a
     header but that header unlocks nothing.
  4. Audit rows carry BOTH actor_id=platform_admin_id AND
     impersonating_id=target_user_id.
  5. stop_impersonation writes an audit row even if the cookie is expired
     (best-effort; re-reads the target tenant from the JWT claims).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import jwt

from polaris_api.config import get_settings
from polaris_api.services.audit import record as audit_record

logger = logging.getLogger(__name__)

COOKIE_NAME = "pol_impersonation"
COOKIE_DOMAIN = ".getpolaris.my"
COOKIE_MAX_AGE_S = 3600  # 1-hour hard cap per spec


def _signing_secret() -> str:
    s = get_settings().jwt_secret
    if not s:
        raise RuntimeError("jwt_secret is not configured -- cannot sign impersonation cookies")
    return s


def _mint_cookie_value(
    session_id: UUID,
    platform_admin_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
) -> str:
    """Return a signed JWT string suitable for the pol_impersonation cookie.

    Uses PyJWT HS256 so the API (already using pyjwt) can verify without a
    new dependency. The portal verifies with the same shared secret.

    Payload:
      session_id, platform_admin_id, tenant_id, user_id, exp (UTC epoch).
    """
    now = int(time.time())
    exp = now + COOKIE_MAX_AGE_S
    payload: dict[str, Any] = {
        "session_id": str(session_id),
        "platform_admin_id": str(platform_admin_id),
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, _signing_secret(), algorithm="HS256")


def _verify_cookie_value(
    cookie_value: str,
    *,
    allow_expired: bool = False,
) -> dict[str, Any] | None:
    """Decode and verify the cookie JWT. Returns payload dict or None.

    When allow_expired=True the expiry check is skipped so stop_impersonation
    can still emit an audit row for an expired session (invariant #5).
    """
    try:
        decode_options = ({"verify_exp": False} if allow_expired else {})
        payload = jwt.decode(
            cookie_value,
            _signing_secret(),
            algorithms=["HS256"],
            options=decode_options,  # type: ignore[arg-type]
        )
        return payload
    except jwt.PyJWTError as exc:
        logger.warning("impersonation cookie decode failed: %s", exc)
        return None


async def start_impersonation(
    platform_admin_id: UUID,
    target_tenant_id: UUID,
    target_user_id: UUID,
    pool: Any,  # asyncpg.Pool -- avoid import cycle with type annotation
) -> tuple[str, UUID, datetime]:
    """Start an impersonation session.

    Creates a signed cookie value, records an audit event under the
    target tenant's chain, and returns:
      (cookie_value, session_id, expires_at)
    """
    session_id = uuid4()
    expires_at = datetime.fromtimestamp(int(time.time()) + COOKIE_MAX_AGE_S, tz=UTC)

    cookie_value = _mint_cookie_value(
        session_id=session_id,
        platform_admin_id=platform_admin_id,
        tenant_id=target_tenant_id,
        user_id=target_user_id,
    )

    # Write audit event under the target tenant's chain.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await audit_record(
                conn,
                tenant_id=target_tenant_id,
                actor=platform_admin_id,
                action="impersonation.started",
                after={
                    "session_id": str(session_id),
                    "platform_admin_id": str(platform_admin_id),
                    "target_user_id": str(target_user_id),
                    "expires_at": expires_at.isoformat(),
                },
                impersonating_kind="platform_admin",
                impersonating_id=platform_admin_id,
            )

    logger.info(
        "impersonation started: admin=%s tenant=%s user=%s session=%s",
        platform_admin_id, target_tenant_id, target_user_id, session_id,
    )
    return cookie_value, session_id, expires_at


async def stop_impersonation(
    cookie_value: str,
    pool: Any,  # asyncpg.Pool
) -> None:
    """Stop an impersonation session. Writes audit event best-effort.

    Invariant #5: always attempts the audit write even if the cookie is
    expired. Raises ValueError if the cookie is completely invalid (bad
    signature or malformed), but never silently swallows errors.
    """
    # Try without expiry check first (so we can audit even for expired sessions).
    payload = _verify_cookie_value(cookie_value, allow_expired=True)
    if payload is None:
        raise ValueError("pol_impersonation cookie has invalid signature or is malformed")

    platform_admin_id = UUID(payload["platform_admin_id"])
    tenant_id = UUID(payload["tenant_id"])
    user_id = UUID(payload["user_id"])
    session_id = payload["session_id"]

    # Write audit event best-effort.
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await audit_record(
                    conn,
                    tenant_id=tenant_id,
                    actor=platform_admin_id,
                    action="impersonation.stopped",
                    after={
                        "session_id": session_id,
                        "platform_admin_id": str(platform_admin_id),
                        "target_user_id": str(user_id),
                    },
                    impersonating_kind="platform_admin",
                    impersonating_id=platform_admin_id,
                )
    except Exception:
        logger.exception(
            "stop_impersonation: audit write failed for session=%s -- "
            "cookie will still be cleared",
            session_id,
        )
        # Do NOT swallow -- re-raise so the caller can decide whether to
        # surface the error. The cookie clearing happens in the router
        # regardless (belt-and-braces; clearing is idempotent).
        raise

    logger.info(
        "impersonation stopped: admin=%s tenant=%s user=%s session=%s",
        platform_admin_id, tenant_id, user_id, session_id,
    )
