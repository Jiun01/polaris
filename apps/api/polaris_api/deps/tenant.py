"""TenantContext FastAPI dependency — Phase 1 PR-2.

Every request-scoped router consumes `Depends(get_tenant_context)`. The
dependency:

1. Pulls a Bearer token from the `Authorization` header.
2. Decodes it as HS256 using `JWT_SECRET`; verifies `iss`, `exp`, `iat`.
3. Acquires a connection from `anon_pool` and opens a transaction.
4. `SELECT set_config('app.current_tenant_id', $1, true)` — binds the
   tenant id to the session for the duration of this transaction. The
   `is_local=true` third arg is the function-form equivalent of `SET LOCAL`.
   Statement-form `SET LOCAL` can't bind asyncpg `$1` placeholders — see
   docs/Polaris V2/04b_PHASE_1_DEEP.md §9 gotcha #9.
5. Loads the tenant row to validate status. Suspended (`payment_overdue`)
   tenants are rejected with `403 service_suspended`; the response would
   carry the unpaid-invoice payment URL once Phase 10's billing layer ships
   (currently a plain message). Suspended `quota_exhausted` is the same — the
   bot is off but Chatwoot stays active per ADR-018; the routing branch sits
   in Phase 2's webhook dispatcher, not here.

The connection + transaction are exposed via `get_request_conn` so downstream
deps that need to run their own SQL inherit the same `app.current_tenant_id`
GUC. FastAPI's Depends cache ensures `get_tenant_context` and
`get_request_conn` share one connection per request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request, status

from polaris_api.config import get_settings
from polaris_api.db.pools import get_anon_pool

# Request.state attribute name we use to stash the per-request connection so
# `get_tenant_context` and `get_request_conn` interlock cleanly even if
# FastAPI ever stops caching Depends results.
_CONN_STATE_KEY = "polaris_db_conn"

# Max seconds to wait for a free pooled connection before treating the backend
# as transiently unavailable (QA finding M5). Generous vs the sub-ms healthy
# case; tight enough that a pooler outage fails fast with a 503 + Retry-After
# rather than blocking the worker and stacking requests.
_ACQUIRE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class TenantContext:
    """Resolved tenant + user identity for one request.

    Frozen because handler code should never mutate the context. If a handler
    needs to switch tenants (admin impersonation), it does so through a
    distinct dependency that constructs a new TenantContext explicitly.
    """

    tenant_id: UUID
    user_id: UUID
    role: str
    plan_tier: str
    status: str


async def get_request_conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """Yield a request-scoped asyncpg.Connection inside an open transaction.

    The connection is acquired from the anon pool and a transaction is opened
    before yield; the transaction commits on clean exit, rolls back on
    exception. `app.current_tenant_id` is set by `get_tenant_context` once
    the JWT has been decoded — handlers that take both deps see the GUC
    already in place.

    A failure to *acquire* a connection — the Supabase/Supavisor pooler
    unreachable (``{:error, :econnrefused}``) or the acquire timing out — is a
    transient backend-dependency outage, not a request bug. Surface it as a
    retryable 503 instead of a raw 500 so a pooler blip doesn't spam Sentry
    with unhandled errors (the portal polls notifications every 30 s) and
    clients get a Retry-After to back off. Errors raised *after* the connection
    is acquired (handler/query errors) propagate normally so the transaction
    rolls back and genuine bugs still surface as 500s.
    """
    pool = get_anon_pool()
    try:
        # Bound the wait so a Supavisor blip surfaces as a retryable 503 instead
        # of hanging the request indefinitely (QA finding M5). An exhausted pool
        # or unreachable backend raises asyncio.TimeoutError (== TimeoutError on
        # 3.11+), which the handler below already maps to 503 Retry-After.
        conn = await pool.acquire(timeout=_ACQUIRE_TIMEOUT_S)
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DB_UNAVAILABLE",
                "message": "database temporarily unavailable; please retry",
            },
            headers={"Retry-After": "5"},
        ) from exc
    try:
        async with conn.transaction():
            # Stash on request.state so debugging / middleware can inspect it.
            setattr(request.state, _CONN_STATE_KEY, conn)
            yield conn
    finally:
        await pool.release(conn)


def _decode_jwt(token: str) -> dict[str, Any]:
    """Decode + verify a Polaris-issued JWT. Raises HTTPException(401) on any
    validation failure — we never tell the client *why* a token is invalid
    (information leakage about secret rotation, clock drift, claim shape)."""
    settings = get_settings()
    if not settings.jwt_secret:
        # Config error, not an auth failure — surface as 500.
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
            options={"require": ["exp", "iat", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _extract_bearer(request: Request) -> str:
    """Pull the Bearer token from Authorization. 401 if absent / malformed."""
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_bearer_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


async def get_tenant_context(
    request: Request,
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantContext:
    """Resolve the per-request tenant + user, set the RLS GUC, return context."""
    token = _extract_bearer(request)
    claims = _decode_jwt(token)

    raw_tenant_id = claims.get("tenant_id")
    raw_user_id = claims.get("sub")
    role = claims.get("role", "admin")
    if not raw_tenant_id or not raw_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        tenant_id = UUID(str(raw_tenant_id))
        user_id = UUID(str(raw_user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Set the GUC inside the open transaction. set_config(name, value, is_local)
    # — is_local=true is the equivalent of SET LOCAL. Postgres now applies RLS
    # against this value for every subsequent query on this connection until
    # the transaction ends.
    await conn.execute(
        "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_id)
    )

    # Load tenant row to validate. This SELECT goes through RLS — only the
    # tenant's own row is visible. A missing row means either the tenant was
    # deleted between token issuance and this request, or the token is for a
    # tenant_id that doesn't exist (which only happens if the secret leaked
    # and an attacker forged a token).
    row = await conn.fetchrow(
        "SELECT status, plan_tier, suspended_reason FROM tenants WHERE id = $1",
        tenant_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found"
        )
    if row["status"] != "active":
        # Both quota_exhausted and payment_overdue land here. Phase 2's webhook
        # dispatcher branches on suspended_reason for routing (bot off vs full
        # lockdown). For this dep — which fronts portal + admin routes — any
        # non-active status is a 403.
        detail = (
            f"service_suspended:{row['suspended_reason']}"
            if row["suspended_reason"]
            else "service_suspended"
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role=str(role),
        plan_tier=str(row["plan_tier"]),
        status=str(row["status"]),
    )
