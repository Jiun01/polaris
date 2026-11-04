"""Tenant-facing Messenger Profile API — Phase 7 PR-5.

Mounted at ``/api/tenant/channels/{channel_id}/messenger-profile``.

Four endpoints per 04h §5.6:

  * ``GET    /``       — read current saved DB config + last_pushed_at /
                         last_push_error for portal display
  * ``PUT    /``       — save bundle in DB + immediately push to Meta
  * ``POST   /push``   — re-push the saved DB bundle without editing
  * ``POST   /unset``  — DELETE specific fields on Meta side (Q-PHASE7-06
                         tenants who fully clear a section need this
                         because POST is merge-only)

Auth: standard ``get_tenant_context`` flow so channel_id 404s when not
owned by the calling tenant (RLS strips it).

**TODO: rate-limit ``push`` / ``unset`` paths** at 1 per 10s per channel —
Meta's Messenger Profile endpoint quotas are not strict but a
misbehaving portal client could fan out and trip them. The rate-limit
infrastructure (slowapi / Redis token bucket) is not wired into the
codebase at GA, so this is deferred to a follow-up. Until then, Meta's
own quotas are the backstop.

Error envelope codes:

  * ``MP_CHANNEL_NOT_FOUND``     — channel_id missing or wrong tenant
  * ``MP_PAGE_NOT_CONFIGURED``   — fb_page_id NULL on the channel row
  * ``MP_TOKEN_MISSING``         — tenant_secrets has no page token
  * ``MP_META_ERROR``            — Meta rejected the request; details
                                   carry Meta's error envelope verbatim
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from polaris_shared_types.messenger_profile import (
    MessengerProfileBundle,
    MessengerProfilePushResult,
)

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.messenger_profile import (
    MessengerProfileConfigError,
    MessengerProfileStructuralError,
    MessengerProfileTransientError,
    delete_profile_fields,
    push_profile,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-messenger-profile"])


def _config_error_to_http(exc: MessengerProfileConfigError) -> HTTPException:
    """Map config-class errors to HTTP 400 with stable envelope codes
    so the portal can branch on the failure mode without parsing
    free-text messages."""
    msg = str(exc)
    if "tenant_channels row not found" in msg:
        code = "MP_CHANNEL_NOT_FOUND"
        http = status.HTTP_404_NOT_FOUND
    elif "fb_page_id missing" in msg:
        code = "MP_PAGE_NOT_CONFIGURED"
        http = status.HTTP_400_BAD_REQUEST
    elif "meta_fb_page_access_token" in msg:
        code = "MP_TOKEN_MISSING"
        http = status.HTTP_400_BAD_REQUEST
    else:
        code = "MP_CONFIG_ERROR"
        http = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=http,
        detail={"code": code, "message": msg},
    )


def _meta_error_to_http(
    exc: MessengerProfileStructuralError | MessengerProfileTransientError,
) -> HTTPException:
    """Map Meta-side errors to HTTP 502 (transient — Meta's fault) or
    HTTP 400 (structural — tenant config / payload issue)."""
    if isinstance(exc, MessengerProfileStructuralError):
        # 401/403 → 502 because the tenant's portal CAN'T fix a token
        # via JSON re-submission (they need to re-OAuth the Page).
        # 400 → 400 because the payload is the tenant's responsibility.
        http = (
            status.HTTP_400_BAD_REQUEST
            if exc.status_code == 400
            else status.HTTP_502_BAD_GATEWAY
        )
    else:
        http = status.HTTP_502_BAD_GATEWAY
    return HTTPException(
        status_code=http,
        detail={
            "code": "MP_META_ERROR",
            "message": str(exc),
            "meta_status": exc.status_code,
        },
    )


# =====================================================================
# Read saved config (GET)
# =====================================================================


_LOAD_SAVED_CONFIG_SQL = """
SELECT
    messenger_icebreakers              AS ice_breakers,
    messenger_persistent_menu          AS persistent_menu,
    messenger_get_started              AS get_started,
    messenger_profile_last_pushed_at   AS last_pushed_at,
    messenger_profile_last_push_error  AS last_push_error
FROM tenant_channels
WHERE id = $1 AND tenant_id = $2
"""


@router.get("/")
async def read_saved_profile(
    channel_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> dict[str, Any]:
    """Read the DB-persisted Messenger Profile config + push metadata.

    Used by the portal to populate the editor. Does NOT call Meta —
    returns whatever is in tenant_channels. The portal can call
    ``GET ?live=true`` (future) to pull from Meta and diff.
    """
    row = await conn.fetchrow(_LOAD_SAVED_CONFIG_SQL, channel_id, ctx.tenant_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "MP_CHANNEL_NOT_FOUND",
                "message": f"channel_id {channel_id} not found",
            },
        )
    return {
        "ice_breakers": row["ice_breakers"] or [],
        "persistent_menu": row["persistent_menu"] or [],
        "get_started": row["get_started"],
        "last_pushed_at": (
            row["last_pushed_at"].isoformat() if row["last_pushed_at"] else None
        ),
        "last_push_error": row["last_push_error"],
    }


# =====================================================================
# Save + push (PUT)
# =====================================================================


_SAVE_BUNDLE_SQL = """
UPDATE tenant_channels
   SET messenger_icebreakers     = $3::jsonb,
       messenger_persistent_menu = $4::jsonb,
       messenger_get_started     = $5::jsonb
 WHERE id = $1 AND tenant_id = $2
"""


@router.put("/")
async def save_and_push(
    channel_id: UUID,
    bundle: MessengerProfileBundle,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> MessengerProfilePushResult:
    """Save the bundle to tenant_channels + immediately push to Meta.

    Atomic with respect to the DB write — if Meta push fails, the DB
    rows still hold the new config + the push-state columns record
    the failure. Tenants can retry via POST /push without re-sending
    the body.
    """
    save_result = await conn.execute(
        _SAVE_BUNDLE_SQL,
        channel_id,
        ctx.tenant_id,
        # Pass lists/dict DIRECTLY — the JSONB codec (pools.py) serialises ONCE.
        # json.dumps(...) here would double-encode into a JSONB string/array-of-
        # one-string, breaking jsonb reads (see services/audit.py).
        [ib.model_dump() for ib in bundle.ice_breakers],
        [pm.model_dump() for pm in bundle.persistent_menu],
        bundle.get_started.model_dump() if bundle.get_started else None,
    )
    if save_result.endswith(" 0"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "MP_CHANNEL_NOT_FOUND",
                "message": f"channel_id {channel_id} not found",
            },
        )

    try:
        return await push_profile(
            tenant_id=ctx.tenant_id,
            tenant_channel_id=channel_id,
            conn=conn,
        )
    except MessengerProfileConfigError as exc:
        raise _config_error_to_http(exc) from exc
    except (
        MessengerProfileStructuralError,
        MessengerProfileTransientError,
    ) as exc:
        raise _meta_error_to_http(exc) from exc


# =====================================================================
# Re-push (POST /push)
# =====================================================================


@router.post("/push")
async def repush(
    channel_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> MessengerProfilePushResult:
    """Push the saved DB bundle to Meta without editing.

    Used after a transient Meta outage when the tenant wants to retry
    without re-submitting the bundle. Also used by the portal's
    "Re-sync with Meta" button.
    """
    try:
        return await push_profile(
            tenant_id=ctx.tenant_id,
            tenant_channel_id=channel_id,
            conn=conn,
        )
    except MessengerProfileConfigError as exc:
        raise _config_error_to_http(exc) from exc
    except (
        MessengerProfileStructuralError,
        MessengerProfileTransientError,
    ) as exc:
        raise _meta_error_to_http(exc) from exc


# =====================================================================
# Unset fields (POST /unset)
# =====================================================================


class _UnsetRequest(MessengerProfileBundle):
    """Reused as the request shape; only ``fields`` is read."""


@router.post("/unset")
async def unset(
    channel_id: UUID,
    fields: list[str],
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> dict[str, Any]:
    """DELETE specific Messenger Profile sections on Meta + clear the
    matching DB columns.

    Required to fully clear a section — Meta's POST is merge-only and
    sending an empty list doesn't reliably clear (Meta treats empty
    as "no change" for some sections).

    Allowed field values: ``ice_breakers``, ``persistent_menu``,
    ``get_started``. Other Meta fields are out-of-scope at GA.
    """
    allowed = {"ice_breakers", "persistent_menu", "get_started"}
    bad = [f for f in fields if f not in allowed]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MP_INVALID_FIELDS",
                "message": f"unsupported fields: {bad}",
            },
        )

    try:
        await delete_profile_fields(
            tenant_id=ctx.tenant_id,
            tenant_channel_id=channel_id,
            fields=fields,
            conn=conn,
        )
    except MessengerProfileConfigError as exc:
        raise _config_error_to_http(exc) from exc
    except (
        MessengerProfileStructuralError,
        MessengerProfileTransientError,
    ) as exc:
        raise _meta_error_to_http(exc) from exc

    # Clear matching DB columns so the saved state stays consistent
    # with Meta. (The portal could also issue a PUT with an empty
    # bundle, but unset is the GA-shipped path.)
    #
    # silent-failure-hunter MEDIUM: Meta DELETE has already succeeded
    # above. If the DB UPDATE here throws, Meta and DB diverge — Meta
    # has no sections, DB still holds the old config. Tenant sees a
    # stale portal view + a subsequent push would re-send the stale
    # config to Meta. Wrap with try/except and surface the split-brain
    # in the response so the portal can re-issue PUT to converge.
    # column_map values are literal SQL fragments (no user input —
    # f is already validated against the allowlist above), so the
    # f-string SET clause is safe (S608 suppression is structural).
    column_map = {
        "ice_breakers": "messenger_icebreakers = '[]'::jsonb",
        "persistent_menu": "messenger_persistent_menu = '[]'::jsonb",
        "get_started": "messenger_get_started = NULL",
    }
    sets = ", ".join(column_map[f] for f in fields)
    db_synced = True
    try:
        await conn.execute(
            f"UPDATE tenant_channels SET {sets} "  # noqa: S608 — see comment above
            "WHERE id = $1 AND tenant_id = $2",
            channel_id,
            ctx.tenant_id,
        )
    except Exception:  # noqa: BLE001 — Meta side already succeeded; don't 500
        db_synced = False
        logger.exception(
            "MP_SPLIT_BRAIN: messenger_profile DELETE succeeded on Meta "
            "but DB UPDATE failed — Meta has cleared %s for channel=%s "
            "tenant=%s but DB still holds the old config. Portal should "
            "re-issue PUT to converge.",
            fields,
            channel_id,
            ctx.tenant_id,
        )

    return {"unset": fields, "db_synced": db_synced}
