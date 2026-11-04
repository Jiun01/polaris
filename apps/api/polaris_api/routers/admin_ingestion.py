"""Service-role-only admin routes for ingestion config — Phase 3 PR-1.

Mounted at ``/api/admin`` (per Q-PHASE3-06 prefix layering). PR-3 will
extend this router with ``POST /ingestion/force-run``; PR-1 ships only
the desktop's per-tenant chunking-config read.

The desktop (Polaris Ingestion System — out of polaris monorepo per
ADR-057) calls ``GET /tenants/{tenant_id}/chunking-config`` at the
start of each request's processing to pick up per-tenant overrides
(ADR-032 Amendment + Q-PHASE3-15: chunk_size / chunk_overlap /
contextual_enrichment_default live in ``tenants.settings`` JSONB).

Auth model: a service-role JWT carries ``role='platform_service'`` and
NO ``tenant_id`` claim (the caller IS the platform, not a tenant). The
tenant_admin JWT issued by the portal carries ``role='admin'`` +
``tenant_id`` — that token MUST be rejected with 403 from any
``/api/admin/*`` route so a compromised tenant token can never pivot
to admin endpoints. The dependency below decodes the JWT with the
shared JWT_SECRET (HS256) but applies its own role check; it does NOT
chain into ``get_tenant_context`` because admin routes don't carry a
tenant scope.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any
from uuid import UUID

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from polaris_shared_types import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONTEXTUAL_ENRICHMENT,
    ChunkingConfig,
)
from pydantic import BaseModel, Field, ValidationError, model_validator

from polaris_api.config import get_settings
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-ingestion"])

# Roles the platform issues for service-to-service calls. Kept narrow
# on purpose: any future admin role lands here as an explicit append,
# not via a wildcard, so the security review on each PR sees the
# expanded surface.
_ADMIN_ROLES = frozenset({"platform_service", "platform_admin"})

# Force-run is the more sensitive operation — flipping rows changes the
# desktop's drain order. Restrict it to platform_admin (humans), not
# platform_service (machines). The desktop itself never calls force-run.
_FORCE_RUN_ROLES = frozenset({"platform_admin"})

# ADR-032 Amendment defaults — single-sourced in `polaris_shared_types`
# (PR-5 / review M4) so the desktop project picks the same values via
# the shared-types schema export. Migration 002 also defaults the
# `tenants.chunk_size` / `chunk_overlap` columns to 512 / 80; if these
# constants drift from the SQL default, fresh tenants would see one
# value at INSERT time and another via this endpoint.


def _decode_admin_jwt(token: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.jwt_secret:
        # Config error, surface as 500 — same shape as deps/tenant.py.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="server misconfigured: jwt_secret unset",
        )
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "iat", "iss", "role"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_platform_admin(request: Request) -> str:
    """Stricter gate than ``require_platform_service`` — humans only.

    Used by mutating endpoints (force-run flips queue ordering) where
    automation should never be the caller. Same 401-vs-403 distinction
    as the service gate so the portal can route the user correctly.
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_bearer_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = _decode_admin_jwt(token)
    role = str(claims.get("role", ""))
    if role not in _FORCE_RUN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="platform_admin_required"
        )
    # Stash the actor's user_id on the request so the handler can read it
    # for audit-log writes without re-decoding the JWT. PyJWT's `sub` claim
    # is the standard "subject" — for platform_admin tokens this is the
    # admin user's UUID; for platform_service tokens it's the desktop's
    # service-account id.
    raw_sub = claims.get("sub")
    if raw_sub:
        try:
            request.state.admin_actor_id = UUID(str(raw_sub))
        except ValueError:
            # Bad `sub` claim — don't crash the handler, just leave actor=None
            # in the audit row. Logged for ops to notice.
            logger.warning(
                "platform_admin JWT had non-UUID sub claim — audit actor will be NULL"
            )
            request.state.admin_actor_id = None
    else:
        request.state.admin_actor_id = None
    return role


def require_platform_service(request: Request) -> str:
    """Gate: caller must present a service-role JWT.

    Tenant JWTs (``role='admin'``) get a 403, NOT a 401, so the portal
    can distinguish "token is bad" from "token is fine but wrong scope".
    A logged-in tenant_admin who stumbles onto an admin URL sees "you
    don't have access" rather than "you're logged out".
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_bearer_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = _decode_admin_jwt(token)
    role = str(claims.get("role", ""))
    if role not in _ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin_role_required"
        )
    return role


@router.get(
    "/tenants/{tenant_id}/chunking-config",
    response_model=ChunkingConfig,
    summary="Read a tenant's chunking config (desktop ingestion worker only)",
)
async def get_chunking_config(
    tenant_id: UUID,
    _role: Annotated[str, Depends(require_platform_service)],
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
) -> ChunkingConfig:
    """Read ``tenants.settings`` JSONB and project chunking knobs.

    Per Q-PHASE3-15 chunk settings live in ``tenants.settings`` JSONB
    (not as dedicated columns) so the Phase 8 portal can add new knobs
    without migrations. Missing values fall back to ADR-032 Amendment
    defaults. Returns 404 if the tenant doesn't exist.

    Uses the BYPASSRLS service pool — ``tenants`` has a
    ``self_select`` RLS policy keyed on the per-tenant GUC, which
    isn't set on platform-scope admin calls. PR-4 fixed this; the
    PR-1 version used the anon pool and silently 404'd every call
    in production (mocked tests masked it).
    """
    row = await conn.fetchrow(
        "SELECT settings FROM tenants WHERE id = $1", tenant_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found"
        )
    settings_jsonb = row["settings"] or {}
    if not isinstance(settings_jsonb, dict):
        # Schema drift defence: settings should always be a JSON object.
        # A list / scalar would crash the .get below.
        logger.warning(
            "chunking_config: tenant %s settings is not an object (%s)",
            tenant_id, type(settings_jsonb).__name__,
        )
        settings_jsonb = {}
    try:
        return _build_chunking_config(tenant_id, settings_jsonb)
    except HTTPException:
        raise


class ForceRunRequest(BaseModel):
    """Request body for POST /api/admin/ingestion/force-run.

    Three modes:
      * ``request_id`` set → flip just that one row.
      * ``tenant_id`` set → flip every `pending` row for that tenant.
      * neither set AND ``confirm_global=true`` → flip every `pending`
        row across all tenants (ops "drain everything now" drill).
        ``confirm_global`` defaults to ``False`` so a typo'd empty body
        produces 422 instead of accidentally re-prioritising the whole
        platform.

    Passing both is allowed but redundant; the row predicate also asserts
    tenant_id (belt-and-braces against typo'd cross-tenant flips).
    """

    tenant_id: UUID | None = None
    request_id: UUID | None = None
    # PR-4 H1 fix: ops must explicitly opt into the platform-wide hammer.
    # Without this flag, an empty body 422s rather than silently flipping
    # every tenant's queue.
    confirm_global: bool = False

    @model_validator(mode="after")
    def _require_scope_or_confirm(self) -> ForceRunRequest:
        if self.tenant_id is None and self.request_id is None and not self.confirm_global:
            raise ValueError(
                "force-run with no tenant_id or request_id requires "
                "confirm_global=true (platform-wide flip is intentional, "
                "not the default)"
            )
        return self


class ForceRunResponse(BaseModel):
    """Per PRD §6.2.1: counts of rows whose priority flipped + a token
    the desktop / operator can correlate with the next run cycle."""

    accepted: int = Field(ge=0)
    run_token: UUID


@router.post(
    "/ingestion/force-run",
    response_model=ForceRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Flip pending ingestion rows to high priority for next drain (PRD §6.2.1)",
)
async def force_run(
    body: ForceRunRequest,
    request: Request,
    _role: Annotated[str, Depends(require_platform_admin)],
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
) -> ForceRunResponse:
    """Flag pending rows as high priority.

    The desktop's pop_pending() orders by (priority DESC, submitted_at
    ASC), so flagged rows drain on the next cron tick / GUI invocation /
    (v2.1) wake-on-submission tunnel. We do NOT trigger the desktop here
    — the workstation may be offline. The 202 + run_token tell the
    operator "we acknowledged your request; check back in `<cron
    interval>`".

    Audit trail (PR-4 H2 fix): every affected tenant gets one
    ``audit_logs`` row with action=``ingestion.force_run``, actor=the
    platform_admin's user_id from the JWT ``sub`` claim, payload
    carrying the run_token + count for forensic correlation.

    All UPDATE branches filter ``priority = 'normal'`` (PR-4 M2 fix) so
    re-running force-run on rows already at high doesn't burn txids /
    WAL on no-op writes.
    """
    run_token = uuid.uuid4()
    actor_id: UUID | None = getattr(request.state, "admin_actor_id", None)

    # Use RETURNING tenant_id to discover which tenants were actually
    # affected — `audit_logs` is per-tenant-chain, so we need one record()
    # call per distinct tenant. BYPASSRLS lets the service pool see every
    # tenant; the platform_admin gate above already confirmed the caller
    # is authorised to act platform-wide.
    if body.request_id is not None:
        if body.tenant_id is not None:
            # Row-scoped with belt-and-braces tenant assertion.
            sql = """
                UPDATE ingestion_requests
                   SET priority = 'high'
                 WHERE id = $1
                   AND tenant_id = $2
                   AND status = 'pending'
                   AND priority = 'normal'
                RETURNING tenant_id
            """
            affected_tenants = await conn.fetch(
                sql, body.request_id, body.tenant_id
            )
        else:
            sql = """
                UPDATE ingestion_requests
                   SET priority = 'high'
                 WHERE id = $1
                   AND status = 'pending'
                   AND priority = 'normal'
                RETURNING tenant_id
            """
            affected_tenants = await conn.fetch(sql, body.request_id)
    elif body.tenant_id is not None:
        sql = """
            UPDATE ingestion_requests
               SET priority = 'high'
             WHERE tenant_id = $1
               AND status = 'pending'
               AND priority = 'normal'
            RETURNING tenant_id
        """
        affected_tenants = await conn.fetch(sql, body.tenant_id)
    else:
        # confirm_global=true was already enforced by the model validator.
        sql = """
            UPDATE ingestion_requests
               SET priority = 'high'
             WHERE status = 'pending'
               AND priority = 'normal'
            RETURNING tenant_id
        """
        affected_tenants = await conn.fetch(sql)

    affected = len(affected_tenants)

    # Audit one row per distinct tenant. Multiple flipped rows for the
    # same tenant collapse into one audit entry — the run_token in the
    # payload lets forensic queries join back to the original request.
    distinct_tenants: set[UUID] = {row["tenant_id"] for row in affected_tenants}
    async with conn.transaction():
        for affected_tenant_id in distinct_tenants:
            tenant_row_count = sum(
                1 for row in affected_tenants if row["tenant_id"] == affected_tenant_id
            )
            await audit_record(
                conn,
                tenant_id=affected_tenant_id,
                actor=actor_id,
                action="ingestion.force_run",
                before={"priority": "normal"},
                after={
                    "priority": "high",
                    "run_token": str(run_token),
                    "rows_flipped": tenant_row_count,
                    "scope": (
                        "row" if body.request_id is not None
                        else "tenant" if body.tenant_id is not None
                        else "global"
                    ),
                },
            )

    logger.info(
        "force_run accepted actor=%s tenant_id=%s request_id=%s "
        "global=%s affected=%d distinct_tenants=%d run_token=%s",
        actor_id, body.tenant_id, body.request_id, body.confirm_global,
        affected, len(distinct_tenants), run_token,
    )
    return ForceRunResponse(accepted=affected, run_token=run_token)


def _build_chunking_config(
    tenant_id: UUID, settings_jsonb: dict[str, Any]
) -> ChunkingConfig:
    """Construct ChunkingConfig with ADR-032 Amendment defaults.

    Extracted from the endpoint so force-run + chunking-config don't
    duplicate the try/except shape. Bad settings raise HTTPException(500)
    directly because the caller chains it from inside the handler.
    """
    try:
        return ChunkingConfig(
            chunk_size=int(settings_jsonb.get("rag_chunk_size", DEFAULT_CHUNK_SIZE)),
            chunk_overlap=int(
                settings_jsonb.get("rag_chunk_overlap", DEFAULT_CHUNK_OVERLAP)
            ),
            contextual_enrichment_default=bool(
                settings_jsonb.get(
                    "rag_contextual_enrichment", DEFAULT_CONTEXTUAL_ENRICHMENT
                )
            ),
        )
    except ValidationError as exc:
        # Bad settings JSONB (overlap >= size, out-of-range value, etc.) is a
        # platform-side data error — an admin mis-edit, not the desktop's
        # fault. Surface as 500 so ops notices in dashboards; keep the tenant
        # facing the same generic "try again" rather than leaking internals.
        logger.error(
            "chunking_config: invalid settings for tenant %s: %s",
            tenant_id, exc.errors(),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="invalid_chunking_config",
        ) from exc
