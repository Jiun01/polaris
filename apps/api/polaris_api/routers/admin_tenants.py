"""Service-role-only admin routes for tenant lifecycle — Phase 9 PR-3.

Mounted at ``/api/admin/tenants`` per the standard prefix layering.

CRUD/lifecycle only — channel management (``admin_tenant_channels.py``) and
email inbox config (``admin_tenant_email.py``) were split out in Batch E
(task E4; see PRP plan audit-batch-e-structural-splits) and mount at the
same ``/api/admin/tenants`` prefix, mirroring the existing
``admin_tenants_router`` / ``admin_ea_router`` precedent. Route paths are
byte-identical to before the split — only which ``APIRouter()`` instance owns
them changed.

Endpoints (this file):

  * ``GET    /``                           — list with optional ?status filter
  * ``POST   /``                           — create wizard backend; INSERTs
                                             tenants + tenant_users rows, emits
                                             DSAR-friendly audit event, returns
                                             {id, slug} for the admin UI to
                                             redirect into the detail page
  * ``GET    /{tenant_id}``                — detail; supports ?preview=delete
                                             to return cascade-scope counts
                                             the delete confirmation needs
  * ``POST   /{tenant_id}/suspend``        — manual suspend (records reason)
  * ``POST   /{tenant_id}/unsuspend``      — manual unsuspend
  * ``DELETE /{tenant_id}``                — soft-delete (per ADR-029 30-day grace);
                                             flips status='deleted', records DSAR
                                             audit row. Physical purge runs in a
                                             Phase 10 retention sweeper.

Auth: ``require_platform_admin`` (mirrors admin_ingestion.py shape) —
the JWT must carry ``role='platform_admin'`` with no tenant_id claim. A
compromised tenant_admin JWT cannot pivot to ``/api/admin/*``. Also
re-exported for ``admin_tenant_channels.py`` / ``admin_tenant_email.py`` /
``admin_billing.py`` (existing precedent) to import as their own
``Depends()``.

Backing tables:
  - tenants (migration 002)
  - tenant_users (migration 003)
  - tenant_billing (migration 008) — read for MRR
  - tenant_invoices (migrations 008 + 054) — cascade-scope count
  - platform_admins (migration 054) — actor lookup for audit
  - tenant_quotas (migration 007) — tier cap enforcement

ADR anchors:
  - ADR-012 — sales-only signup; this is the SOLE provisioning path
  - ADR-015 Amendment 3rd — channel tier caps + override (now in
    admin_tenant_channels.py, but ``_CANONICAL_CHANNEL_KINDS`` /
    ``_TIER_QUOTA_DEFAULTS`` stay here — see below)
  - ADR-029 — tenant lifecycle (soft-delete with 30-day grace; physical
    purge in retention sweeper, not here)
  - ADR-030 — auth + audit hash chain (every write emits an audit row
    via services.audit.record)
  - ADR-049 — dual-path suspension; manual suspends record reason
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from polaris_shared_types import DEFAULT_RETRIEVAL_TOOLS, AgentConfig
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from polaris_api.config import get_settings
from polaris_api.services import secrets as secrets_svc
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record
from polaris_api.services.booking.account_provisioning import (
    create_ea_account,
    deprovision_ea_account,
    split_display_name,
)
from polaris_api.services.channels.secret_kinds import BASE_CHANNEL_SECRET_KINDS
from polaris_api.services.demo_sessions import POLARIS_INTERNAL_TENANT_ID
from polaris_api.services.portal_invite import issue_portal_invite

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-tenants"])

_FORBID = ConfigDict(extra="forbid")


_ADMIN_ROLES = frozenset({"platform_admin"})


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$")


_VALID_TIERS = ("starter", "pro", "ultra", "enterprise")


_VALID_CYCLES = ("monthly", "annual")


_CANONICAL_CHANNEL_KINDS: frozenset[str] = frozenset(
    {"whatsapp", "instagram_login", "messenger", "telegram", "email"}
)


_VALID_SUSPEND_REASONS = ("quota_exhausted", "payment_overdue", "manual")


def _assert_not_gateway(tenant_id: UUID) -> None:
    """Refuse destructive lifecycle ops on the polaris-internal demo gateway.

    The gateway (fixed UUID ``POLARIS_INTERNAL_TENANT_ID``) owns the live demo
    channels + the shared sales KB and has no self-serve recreate path, so
    deleting or purging it silently breaks the demo system. Mirrors the
    exclusion ``admin_demos.py`` already applies to every demo read/write.
    """
    if tenant_id == POLARIS_INTERNAL_TENANT_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="gateway_protected",
        )


# Base map is shared (services.channels.secret_kinds); telegram is a local
# addition here since this map covers every channel kind on the tenant
# detail page's masked-token display, not just the auto-captured three.
_POLARIS_KIND_TO_SECRET: dict[str, str] = {
    **BASE_CHANNEL_SECRET_KINDS,
    "telegram": "telegram_bot_token",
}


_TIER_QUOTA_DEFAULTS: dict[str, dict[str, object]] = {
    "starter": {
        "bot_replies": 1_500,
        "conversation_retention_days": 90,
        "allowed_channel_kinds": ["whatsapp"],
        "outbound_broadcast_enabled": False,
        "multi_branch_enabled": False,
        "knowledge_chunks_total_mb": 100,
        "product_images_total_mb": 100,
        "rate_limit_per_minute": 60,
        "monthly_llm_tokens_in": 500_000,
        "monthly_llm_tokens_out": 500_000,
        "monthly_ingestion_pages": 500,
        "monthly_broadcast_recipients": 5_000,
        "monthly_bookings": 500,
        "bot_replies_period_cap": 1_500,
    },
    "pro": {
        "bot_replies": 5_000,
        "conversation_retention_days": 180,
        "allowed_channel_kinds": ["whatsapp", "instagram_login", "messenger", "telegram", "email"],
        "outbound_broadcast_enabled": True,
        "multi_branch_enabled": False,
        "knowledge_chunks_total_mb": 500,
        "product_images_total_mb": 500,
        "rate_limit_per_minute": 120,
        "monthly_llm_tokens_in": 2_000_000,
        "monthly_llm_tokens_out": 2_000_000,
        "monthly_ingestion_pages": 2_000,
        "monthly_broadcast_recipients": 25_000,
        "monthly_bookings": 2_000,
        "bot_replies_period_cap": 5_000,
    },
    "ultra": {
        "bot_replies": 15_000,
        "conversation_retention_days": 365,
        "allowed_channel_kinds": ["whatsapp", "instagram_login", "messenger", "telegram", "email"],
        "outbound_broadcast_enabled": True,
        "multi_branch_enabled": True,
        "knowledge_chunks_total_mb": 2_000,
        "product_images_total_mb": 5_120,
        "rate_limit_per_minute": 300,
        "monthly_llm_tokens_in": 5_000_000,
        "monthly_llm_tokens_out": 5_000_000,
        "monthly_ingestion_pages": 10_000,
        "monthly_broadcast_recipients": 60_000,
        "monthly_bookings": 10_000,
        "bot_replies_period_cap": 15_000,
    },
    "enterprise": {
        "bot_replies": 50_000,  # custom contract; sane default cap, admin overrides post-provision
        "conversation_retention_days": 365,
        "allowed_channel_kinds": ["whatsapp", "instagram_login", "messenger", "telegram", "email"],
        "outbound_broadcast_enabled": True,
        "multi_branch_enabled": True,
        "knowledge_chunks_total_mb": 10_000,
        "product_images_total_mb": 5_120,
        "rate_limit_per_minute": 600,
        "monthly_llm_tokens_in": 10_000_000,
        "monthly_llm_tokens_out": 10_000_000,
        "monthly_ingestion_pages": 50_000,
        "monthly_broadcast_recipients": 100_000,
        "monthly_bookings": 50_000,
        "bot_replies_period_cap": 50_000,
    },
}


def _decode_admin_jwt(token: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.jwt_secret:
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


def require_platform_admin(request: Request) -> UUID:
    """Reject anything that isn't a human platform_admin JWT.

    Returns the admin's platform_admins.id (UUID) for use as the audit
    actor. The token's ``sub`` claim carries the platform_admin_id
    (minted by the admin app's NextAuth jwt callback in PR-2).
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = _decode_admin_jwt(auth.split(" ", 1)[1].strip())
    role = payload.get("role")
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    # Prefer the explicit ``platform_admin_id`` claim minted by the admin
    # app's NextAuth jwt callback (apps/admin/lib/auth/options.ts) — that
    # value is the authoritative platform_admins.id. Fall back to ``sub``
    # for the rare case where NextAuth has set ``sub`` but not the custom
    # claim (e.g. raw test tokens that mint a sub-only payload).
    raw = payload.get("platform_admin_id") or payload.get("sub")
    if not isinstance(raw, str):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token"
        ) from exc


class _CreateTenantRequest(BaseModel):
    model_config = _FORBID

    display_name: str = Field(..., min_length=1, max_length=120)
    slug: str = Field(..., min_length=3, max_length=48)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(None, max_length=40)
    vertical: str | None = Field(None, max_length=80)
    plan_tier: Literal["starter", "pro", "ultra", "enterprise"]
    billing_cycle: Literal["monthly", "annual"]
    sales_quote_id: str | None = Field(None, max_length=64)
    admin_email: EmailStr
    admin_display_name: str = Field(..., min_length=1, max_length=120)
    # EA (calendar) login email — gets a password-reset link. None → defaults to
    # admin_email server-side (the wizard pre-fills it but allows divergence).
    ea_email: EmailStr | None = None
    chatwoot_account_id: int | None = Field(None, ge=1)
    # Optional override for tenant_quotas.allowed_channel_kinds. None → use the
    # tier default from _TIER_QUOTA_DEFAULTS. When supplied, must be a non-empty
    # subset of _CANONICAL_CHANNEL_KINDS (deduped, order-preserving).
    allowed_channel_kinds: list[str] | None = None

    @field_validator("allowed_channel_kinds")
    @classmethod
    def _validate_channel_kinds(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not v:
            raise ValueError("allowed_channel_kinds must not be an empty list")
        seen: dict[str, None] = {}
        for kind in v:
            if kind not in _CANONICAL_CHANNEL_KINDS:
                raise ValueError(
                    f"unknown channel kind {kind!r}; allowed: {sorted(_CANONICAL_CHANNEL_KINDS)}"
                )
            seen.setdefault(kind, None)
        return list(seen)


class _CreateTenantResponse(BaseModel):
    model_config = _FORBID
    id: UUID
    slug: str
    magic_link_dispatched: bool
    magic_link_failure_reason: str | None
    ea_account_provisioned: bool = False


class _SuspendRequest(BaseModel):
    model_config = _FORBID
    reason: Literal["quota_exhausted", "payment_overdue", "manual"] = "manual"


class _TenantSummary(BaseModel):
    model_config = _FORBID
    id: UUID
    slug: str
    display_name: str
    status: str
    suspended_reason: str | None
    plan_tier: str | None
    mrr_myr: float | None
    user_count: int
    channel_count: int
    last_inbound_at: str | None
    created_at: str


class _TenantListResponse(BaseModel):
    model_config = _FORBID
    items: list[_TenantSummary]
    total: int


class _CascadeScope(BaseModel):
    model_config = _FORBID
    invoices_to_archive: int
    audit_log_rows: int
    channels: int
    users: int
    branches: int
    promotions: int
    audience_members: int


def _row_to_summary(row: asyncpg.Record) -> _TenantSummary:
    # tenants.created_at is NOT NULL (DEFAULT NOW() per migration 002),
    # so _iso() always returns a string here. Cast for mypy.
    created_at = _iso(row["created_at"])
    assert created_at is not None  # noqa: S101 — schema invariant
    return _TenantSummary(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        status=row["status"],
        suspended_reason=row["suspended_reason"],
        plan_tier=row["plan_tier"],
        mrr_myr=float(row["mrr_myr"]) if row["mrr_myr"] is not None else None,
        user_count=row["user_count"] or 0,
        channel_count=row["channel_count"] or 0,
        last_inbound_at=_iso(row["last_inbound_at"]),
        created_at=created_at,
    )


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat()


@router.get("", response_model=_TenantListResponse)
async def list_tenants(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> _TenantListResponse:
    """All tenants, optionally filtered by status. Service-pool (BYPASSRLS)
    so the admin sees every tenant regardless of RLS posture."""
    # Exclude demo tenants (ADR-066): brand demos + the polaris-internal gateway
    # are is_demo=true and must not appear in the customer Tenants list.
    where_clause = "WHERE NOT t.is_demo"
    args: list[Any] = []
    if status_filter:
        if status_filter not in ("active", "suspended", "deleted"):
            raise HTTPException(status_code=400, detail="invalid_status_filter")
        where_clause = "WHERE t.status = $1 AND NOT t.is_demo"
        args.append(status_filter)

    # noqa explanation: ``where_clause`` is one of two literals (empty
    # string OR the exact literal "WHERE t.status = $1") — no user input
    # is interpolated. The status_filter value flows through asyncpg's
    # parameter binding ($1), not the f-string. S608 false positive.
    rows = await conn.fetch(
        f"""
        SELECT
          t.id, t.slug, t.display_name, t.status, t.suspended_reason,
          t.created_at,
          tq.plan_tier                              AS plan_tier,
          tb.mrr_myr                                AS mrr_myr,
          (SELECT COUNT(*) FROM tenant_users  tu WHERE tu.tenant_id    = t.id) AS user_count,
          (SELECT COUNT(*) FROM tenant_channels tc WHERE tc.tenant_id   = t.id) AS channel_count,
          (SELECT MAX(c.last_message_at)
             FROM conversations c WHERE c.tenant_id = t.id)              AS last_inbound_at
        FROM tenants t
        LEFT JOIN tenant_quotas tq ON tq.tenant_id = t.id
        LEFT JOIN tenant_billing tb ON tb.tenant_id = t.id
        {where_clause}
        ORDER BY t.created_at DESC
        LIMIT 500
        """,  # noqa: S608
        *args,
    )
    items = [_row_to_summary(r) for r in rows]
    return _TenantListResponse(items=items, total=len(items))


@router.post("", response_model=_CreateTenantResponse, status_code=201)
async def create_tenant(
    payload: _CreateTenantRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _CreateTenantResponse:
    """Sales-led tenant provisioning per ADR-012.

    Inserts tenants + tenant_quotas (default cap row) + auth.users +
    tenant_users + tenant_billing. Records an audit row. Magic-link
    dispatch is a separate concern: the portal owns its own mailer
    (Phase 8 PR-2) — the admin app just emails the platform_admin out
    of band with the portal URL, OR the operator navigates the new
    admin to portal.getpolaris.my/login.
    """
    if not _SLUG_RE.match(payload.slug):
        raise HTTPException(status_code=400, detail="slug_invalid_format")

    async with conn.transaction():
        # 1. Slug uniqueness — explicit check returns a nicer error than
        # the UNIQUE constraint violation.
        # Exclude soft-deleted tenants: status='deleted' releases the slug so
        # the same slug can be re-provisioned after a tenant is removed.
        exists = await conn.fetchval(
            "SELECT 1 FROM tenants WHERE slug = $1 AND status != 'deleted' LIMIT 1",
            payload.slug,
        )
        if exists:
            raise HTTPException(status_code=409, detail="slug_taken")

        # 2. tenants row.
        # vertical goes into settings JSONB (product-shaped column per ADR);
        # contact_phone goes into tenant_billing (billing-shaped column).
        initial_settings: dict[str, object] = {}
        if payload.vertical:
            initial_settings["vertical"] = payload.vertical
        # Agent-screen redesign 2026-06-21: NEW tenants get the tool-calling agent
        # ON by default with a sensible tool set so the bot works out of the box:
        # answer-from-KB, product catalog lookup, promotions lookup, and handoff.
        # (Retrieval tools default-on per WS2 2026-07-05 — otherwise product/promo
        # answers were dark for every agent-enabled tenant.) Scoped to creation
        # only — existing tenants keep their stored config, and AgentConfig.enabled's
        # Pydantic default stays False (a tenant with no stored config parses to off).
        initial_settings["agent_config"] = AgentConfig(
            enabled=True,
            enabled_tools=["search_knowledge", *DEFAULT_RETRIEVAL_TOOLS, "handoff_to_human"],
        ).model_dump()
        tenant_id = await conn.fetchval(
            """
            INSERT INTO tenants (slug, display_name, status, plan_tier, settings, chatwoot_account_id)
            VALUES ($1, $2, 'active', $3, $4::jsonb, $5)
            RETURNING id
            """,
            payload.slug,
            payload.display_name,
            payload.plan_tier,
            # dict DIRECTLY — JSONB codec serialises once; json.dumps would
            # double-encode into a JSONB string, breaking settings->>'...'
            # reads (see services/audit.py + migration repairing this column).
            initial_settings,
            payload.chatwoot_account_id,
        )

        # 3. tenant_quotas — quota row with tier-specific defaults.
        # All columns are NOT NULL (migration 007); defaults come from
        # _TIER_QUOTA_DEFAULTS keyed on plan_tier.
        q = _TIER_QUOTA_DEFAULTS.get(payload.plan_tier, _TIER_QUOTA_DEFAULTS["starter"])
        # Channel entitlements: explicit override when supplied, else the tier
        # default. asyncpg binds list[str] -> TEXT[] directly (no json.dumps).
        # The tier default is typed `object` (from dict[str, object]); cast it.
        allowed_channel_kinds: list[str] = (
            payload.allowed_channel_kinds
            if payload.allowed_channel_kinds is not None
            else cast("list[str]", q["allowed_channel_kinds"])
        )
        await conn.execute(
            """
            INSERT INTO tenant_quotas (
                tenant_id, plan_tier,
                bot_replies, conversation_retention_days,
                allowed_channel_kinds, outbound_broadcast_enabled,
                multi_branch_enabled, knowledge_chunks_total_mb,
                rate_limit_per_minute, monthly_llm_tokens_in,
                monthly_llm_tokens_out, monthly_ingestion_pages,
                monthly_broadcast_recipients, monthly_bookings,
                bot_replies_period_cap, product_images_total_mb
            ) VALUES (
                $1, $2,
                $3, $4,
                $5, $6,
                $7, $8,
                $9, $10,
                $11, $12,
                $13, $14,
                $15, $16
            )
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            payload.plan_tier,
            q["bot_replies"],
            q["conversation_retention_days"],
            allowed_channel_kinds,
            q["outbound_broadcast_enabled"],
            q["multi_branch_enabled"],
            q["knowledge_chunks_total_mb"],
            q["rate_limit_per_minute"],
            q["monthly_llm_tokens_in"],
            q["monthly_llm_tokens_out"],
            q["monthly_ingestion_pages"],
            q["monthly_broadcast_recipients"],
            q["monthly_bookings"],
            q["bot_replies_period_cap"],
            q["product_images_total_mb"],
        )

        # 4. tenant_billing — tier mirror + plan-change anchor.
        await conn.execute(
            """
            INSERT INTO tenant_billing
              (tenant_id, status, plan_tier, billing_contact_email, billing_contact_phone)
            VALUES ($1, 'active', $2, $3, $4)
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            payload.plan_tier,
            payload.contact_email,
            payload.contact_phone,
        )

        # 5. auth.users — get-or-create via the SECURITY DEFINER helper.
        # polaris_service lacks USAGE on the auth schema so direct
        # SELECT/INSERT on auth.users raises InsufficientPrivilegeError.
        # public.upsert_auth_user_by_email runs as the function owner
        # (postgres) and returns the user id regardless of whether the
        # row already existed.
        user_id = await conn.fetchval(
            "SELECT public.upsert_auth_user_by_email($1)",
            payload.admin_email,
        )

        # 6. tenant_users binding — admin role.
        await conn.execute(
            """
            INSERT INTO tenant_users (tenant_id, user_id, role)
            VALUES ($1, $2, 'admin')
            ON CONFLICT (tenant_id, user_id) DO NOTHING
            """,
            tenant_id,
            user_id,
        )

        # 7. Audit row — chained per-tenant. We record the create BEFORE
        # the SMTP send so the audit trail commits even if the magic-link
        # dispatch later fails outside the transaction. The audit row's
        # ``magic_link_dispatched`` field is updated post-send via a
        # follow-up audit event if desired; for PR-3 the initial record
        # captures the intent (admin_email targeted) and the final
        # outcome is reflected in the response only.
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="tenant.created",
            after={
                "slug": payload.slug,
                "plan_tier": payload.plan_tier,
                "billing_cycle": payload.billing_cycle,
                "admin_email": str(payload.admin_email),
                "sales_quote_id": payload.sales_quote_id,
                "allowed_channel_kinds": list(allowed_channel_kinds),
            },
        )

    # 8. Magic-link dispatch — OUTSIDE the transaction (security review
    # finding #3: holding a DB connection through the SMTP round-trip is
    # an anti-pattern). The token INSERT inside portal_invite still uses
    # the same connection but no longer holds an open transaction — the
    # connection-level statement commits via implicit auto-commit on
    # asyncpg, which is fine for this isolated INSERT. Empty config →
    # silent no-op per services/portal_invite.py.
    invite = await issue_portal_invite(
        conn,
        recipient_email=str(payload.admin_email),
    )

    # 9. Dispatch outcome audit — a SEPARATE per-tenant audit event so
    # the chain reflects what actually happened. Done outside the
    # transaction; uses its own short-lived sub-transaction internally
    # per services.audit.record's contract.
    async with conn.transaction():
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action=(
                "tenant.invite_dispatched"
                if invite.dispatched
                else "tenant.invite_dispatch_skipped"
            ),
            after={
                "magic_link_dispatched": invite.dispatched,
                "magic_link_token_inserted": invite.token_inserted,
                "magic_link_failure_reason": invite.failure_reason,
            },
        )

    # 10. EA account provisioning — OUTSIDE the transaction, same posture as the
    # magic-link: create_ea_account never raises, so a tenant still commits when
    # EA is unreachable. The operator can retry from the tenant-detail page.
    ea_email = str(payload.ea_email or payload.admin_email)
    ea_first, ea_last = split_display_name(payload.admin_display_name)
    ea = await create_ea_account(
        conn,
        tenant_id=tenant_id,
        slug=payload.slug,
        email=ea_email,
        first_name=ea_first,
        last_name=ea_last,
        # Seed EA company_name from the tenant's business name, not the admin's
        # personal name (ea_first/ea_last). Mirrors the tenant-detail re-provision
        # endpoint (admin_ea.py).
        company_name=payload.display_name,
    )
    async with conn.transaction():
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action=(
                "tenant.ea_account_created"
                if ea.provisioned
                else "tenant.ea_account_creation_failed"
            ),
            after={
                "provisioned": ea.provisioned,
                "failure_reason": ea.failure_reason,
                "ea_email": ea_email,
            },
        )

    return _CreateTenantResponse(
        id=tenant_id,
        slug=payload.slug,
        magic_link_dispatched=invite.dispatched,
        magic_link_failure_reason=invite.failure_reason,
        ea_account_provisioned=ea.provisioned,
    )


@router.get("/{tenant_id}")
async def get_tenant(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    preview: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Detail page payload. ``?preview=delete`` returns the cascade-scope
    counts the delete-confirmation needs without firing the destructive
    action."""
    row = await conn.fetchrow(
        """
        SELECT
          t.id, t.slug, t.display_name, t.status, t.suspended_reason,
          t.created_at, t.deleted_at, t.chatwoot_account_id, t.settings,
          tq.plan_tier                              AS plan_tier,
          tb.mrr_myr                                AS mrr_myr,
          tb.company_name                           AS business_name,
          tb.ssm                                    AS ssm,
          tb.billing_contact_email                  AS contact_email,
          tb.billing_contact_phone                  AS contact_phone,
          (SELECT COUNT(*) FROM tenant_users  tu WHERE tu.tenant_id    = t.id) AS user_count,
          (SELECT COUNT(*) FROM tenant_channels tc WHERE tc.tenant_id   = t.id) AS channel_count,
          (SELECT MAX(c.last_message_at)
             FROM conversations c WHERE c.tenant_id = t.id)              AS last_inbound_at
        FROM tenants t
        LEFT JOIN tenant_quotas tq ON tq.tenant_id = t.id
        LEFT JOIN tenant_billing tb ON tb.tenant_id = t.id
        WHERE t.id = $1
        LIMIT 1
        """,
        tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    # Channels (small N — for the admin detail tab).
    # Column aliases: the DB schema uses 'channel' (migration 004); the
    # frontend TenantChannelStub type expects 'kind'. 'connected' maps to
    # status='active'. 'inbox_name' surfaces the Chatwoot inbox ID as a
    # human-readable label (the name isn't stored; ID is good enough for ops).
    # All per-channel connection identifiers are returned so the detail tab can
    # show exactly what was provisioned (CW inbox id, WABA/phone/page/IG ids,
    # WA tier, Telegram bot). The outbound access token itself lives encrypted in
    # tenant_secrets and is surfaced ONLY as a masked last-4 (computed below).
    channels = await conn.fetch(
        """
        SELECT id,
               channel                                    AS kind,
               status,
               (status = 'active')                        AS connected,
               chatwoot_inbox_id,
               'Inbox #' || chatwoot_inbox_id::text       AS inbox_name,
               waba_id,
               phone_number_id,
               fb_page_id,
               ig_business_account_id,
               wa_messaging_tier,
               mm_lite_status,
               telegram_bot_username,
               created_at
        FROM tenant_channels
        WHERE tenant_id = $1
        ORDER BY channel, created_at
        """,
        tenant_id,
    )

    # Masked last-4 of each channel's outbound token, read from tenant_secrets
    # via the channel-kind -> secret-kind map. Best-effort + never the full
    # value: a decrypt failure (or absent secret) yields None so the tab shows
    # "not set" rather than 500-ing the whole detail page.
    channel_token_last4: dict[str, str | None] = {}
    for c in channels:
        secret_kind = _POLARIS_KIND_TO_SECRET.get(c["kind"])
        last4: str | None = None
        if secret_kind is not None:
            try:
                secret_bytes = await secrets_svc.get(conn, tenant_id, secret_kind)
                if secret_bytes:
                    last4 = _mask_last4(secret_bytes.decode())
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "get_tenant: token last4 failed tenant=%s kind=%s err=%s",
                    tenant_id,
                    secret_kind,
                    type(exc).__name__,
                )
        channel_token_last4[str(c["id"])] = last4

    # Recent audit (last 10).
    # LEFT JOIN platform_admins so actor UUID → display_name / email label.
    recent_audit_rows = await conn.fetch(
        """
        SELECT al.id, al.created_at, al.action, al.actor, al.after,
               COALESCE(pa.display_name, pa.email, al.actor::text) AS actor_label
        FROM audit_logs al
        LEFT JOIN platform_admins pa ON pa.id = al.actor
        WHERE al.tenant_id = $1
        ORDER BY al.created_at DESC
        LIMIT 10
        """,
        tenant_id,
    )

    # Primary (earliest 'admin') tenant user — id + email — so the detail
    # page's "Impersonate user" action can deep-link with a concrete target.
    # auth.users is unreadable by polaris_service directly (no auth-schema
    # USAGE), so we go through the SECURITY DEFINER helper (migration 088).
    primary_user = await conn.fetchrow(
        "SELECT user_id, email FROM public.get_tenant_primary_user($1)",
        tenant_id,
    )

    tenant_payload: dict[str, Any] = {
        "id": str(row["id"]),
        "slug": row["slug"],
        "display_name": row["display_name"],
        "primary_user_id": str(primary_user["user_id"]) if primary_user else None,
        "primary_user_email": primary_user["email"] if primary_user else None,
        "status": row["status"],
        "suspended_reason": row["suspended_reason"],
        "plan_tier": row["plan_tier"],
        "mrr_myr": float(row["mrr_myr"]) if row["mrr_myr"] is not None else None,
        "user_count": row["user_count"] or 0,
        "channel_count": row["channel_count"] or 0,
        "last_inbound_at": _iso(row["last_inbound_at"]),
        "created_at": _iso(row["created_at"]),
        "deleted_at": _iso(row["deleted_at"]),
        "chatwoot_account_id": row["chatwoot_account_id"],
        "business_name": row["business_name"],
        "ssm": row["ssm"],
        "contact_email": row["contact_email"],
        "contact_phone": row["contact_phone"],
        "vertical": (row["settings"] or {}).get("vertical")
        if isinstance(row["settings"], dict)
        else None,
        "timezone": (row["settings"] or {}).get("timezone")
        if isinstance(row["settings"], dict)
        else None,
        "ea_account": (row["settings"] or {}).get("ea_account")
        if isinstance(row["settings"], dict)
        else None,
        "channels": [
            {
                "id": str(c["id"]),
                "kind": c["kind"],
                "inbox_name": c["inbox_name"],
                "connected": bool(c["connected"]),
                "status": c["status"],
                "chatwoot_inbox_id": c["chatwoot_inbox_id"],
                "waba_id": c["waba_id"],
                "phone_number_id": c["phone_number_id"],
                "fb_page_id": c["fb_page_id"],
                "ig_business_account_id": c["ig_business_account_id"],
                "wa_messaging_tier": c["wa_messaging_tier"],
                "mm_lite_status": c["mm_lite_status"],
                "telegram_bot_username": c["telegram_bot_username"],
                "access_token_last4": channel_token_last4.get(str(c["id"])),
                "created_at": _iso(c["created_at"]),
            }
            for c in channels
        ],
        "recent_audit": [
            {
                "id": str(a["id"]),
                "occurred_at": _iso(a["created_at"]),
                "action": a["action"],
                "actor_label": a["actor_label"] if a["actor_label"] else "—",
                "after": dict(a["after"]) if a["after"] else None,
            }
            for a in recent_audit_rows
        ],
    }

    if preview == "delete":
        cascade = await _compute_cascade_scope(conn, tenant_id)
        return {"tenant": tenant_payload, "cascade": cascade.model_dump()}

    return tenant_payload


async def _compute_cascade_scope(conn: asyncpg.Connection, tenant_id: UUID) -> _CascadeScope:
    """Count rows that the hard-delete cascade will archive or remove.

    Each table is a single COUNT against an indexed tenant_id column; the
    delete-preview endpoint stays cheap (< 50 ms on a 50-tenant DB).
    """
    counts = await conn.fetchrow(
        """
        SELECT
          (SELECT COUNT(*) FROM tenant_invoices  WHERE tenant_id = $1) AS invoices,
          (SELECT COUNT(*) FROM audit_logs       WHERE tenant_id = $1) AS audit,
          (SELECT COUNT(*) FROM tenant_channels  WHERE tenant_id = $1) AS channels,
          (SELECT COUNT(*) FROM tenant_users     WHERE tenant_id = $1) AS users,
          (SELECT COUNT(*) FROM tenant_branches  WHERE tenant_id = $1) AS branches,
          (SELECT COUNT(*) FROM tenant_promotions WHERE tenant_id = $1) AS promotions,
          (SELECT COUNT(*) FROM audience_members WHERE tenant_id = $1) AS audience
        """,
        tenant_id,
    )
    return _CascadeScope(
        invoices_to_archive=counts["invoices"] or 0,
        audit_log_rows=counts["audit"] or 0,
        channels=counts["channels"] or 0,
        users=counts["users"] or 0,
        branches=counts["branches"] or 0,
        promotions=counts["promotions"] or 0,
        audience_members=counts["audience"] or 0,
    )


class _ChatwootAccountUpdateRequest(BaseModel):
    model_config = _FORBID
    chatwoot_account_id: int = Field(..., ge=1)


@router.patch("/{tenant_id}/chatwoot-account", status_code=200)
async def update_chatwoot_account(
    tenant_id: UUID,
    body: _ChatwootAccountUpdateRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """Set or update the Chatwoot account ID for a tenant.

    Required for inbox lookup, email config, and all Chatwoot-backed
    admin features. Safe to call on any active/suspended/deleted tenant.
    """
    row = await conn.fetchrow("SELECT id FROM tenants WHERE id = $1", tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    await conn.execute(
        "UPDATE tenants SET chatwoot_account_id = $1 WHERE id = $2",
        body.chatwoot_account_id,
        tenant_id,
    )
    await audit_record(
        conn,
        tenant_id=tenant_id,
        actor=admin_id,
        action="tenant.chatwoot_account_updated",
        after={"chatwoot_account_id": body.chatwoot_account_id},
    )
    return {"ok": True, "chatwoot_account_id": body.chatwoot_account_id}


@router.post("/{tenant_id}/suspend", status_code=204)
async def suspend_tenant(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
    body: _SuspendRequest | None = None,
) -> None:
    reason = body.reason if body else "manual"
    async with conn.transaction():
        existing = await conn.fetchrow(
            "SELECT status FROM tenants WHERE id = $1 FOR UPDATE", tenant_id
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="tenant_not_found")
        if existing["status"] == "deleted":
            raise HTTPException(status_code=409, detail="cannot_suspend_deleted")
        await conn.execute(
            """
            UPDATE tenants
               SET status = 'suspended',
                   suspended_reason = $2
             WHERE id = $1
            """,
            tenant_id,
            reason,
        )
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="tenant.suspended",
            after={"reason": reason},
        )


@router.post("/{tenant_id}/unsuspend", status_code=204)
async def unsuspend_tenant(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> None:
    async with conn.transaction():
        existing = await conn.fetchrow(
            "SELECT status FROM tenants WHERE id = $1 FOR UPDATE", tenant_id
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="tenant_not_found")
        if existing["status"] == "deleted":
            raise HTTPException(status_code=409, detail="cannot_unsuspend_deleted")
        await conn.execute(
            """
            UPDATE tenants
               SET status = 'active',
                   suspended_reason = NULL
             WHERE id = $1
            """,
            tenant_id,
        )
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="tenant.unsuspended",
        )


@router.delete("/{tenant_id}", status_code=200)
async def soft_delete_tenant(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """Soft-delete per ADR-029.

    PR-3 implementation flips ``tenants.status`` → ``deleted`` and emits a
    ``tenant.dsar_deleted`` audit row carrying the cascade-scope counts.
    Physical purge (invoice archival to long-term bucket, row removal)
    runs in a Phase 10 retention sweeper after the 30-day grace.
    """
    _assert_not_gateway(tenant_id)
    async with conn.transaction():
        existing = await conn.fetchrow(
            "SELECT status, slug FROM tenants WHERE id = $1 FOR UPDATE",
            tenant_id,
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="tenant_not_found")
        if existing["status"] == "deleted":
            return {"ok": True, "already": "deleted"}

        cascade = await _compute_cascade_scope(conn, tenant_id)

        await conn.execute(
            """
            UPDATE tenants
               SET status     = 'deleted',
                   deleted_at = NOW(),
                   purge_at   = NOW() + INTERVAL '30 days'
             WHERE id = $1
            """,
            tenant_id,
        )
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="tenant.dsar_deleted",
            after={
                "slug": existing["slug"],
                "cascade_scope": cascade.model_dump(),
                "physical_purge_eta": "30_days_per_ADR-029",
            },
        )

    return {"ok": True}


@router.delete("/{tenant_id}/purge", status_code=204)
async def hard_delete_tenant(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> None:
    """Physical purge of a soft-deleted tenant (permanent — no undo).

    The tenant **must** already be soft-deleted (``status='deleted'``).
    Attempting a purge on a live or suspended tenant returns 409.

    The actual purge runs inside the ``admin_hard_delete_tenant(UUID)``
    SECURITY DEFINER function (migration 101). That function executes as its
    owner so it can delete from the append-only ``audit_logs`` table without
    ``polaris_service`` holding a standing DELETE right (ADR-030). The
    FK-dependency-safe deletion order lives in the function body.

    Because the tenant row is destroyed, no ``audit_record`` is written.
    A ``WARNING`` is emitted to the application log instead for operator
    traceability.
    """
    _assert_not_gateway(tenant_id)
    # Validate state with a plain read FIRST so the EA deprovision — which makes
    # an outbound HTTP call (up to ea_api_timeout_s) — runs OUTSIDE the purge
    # transaction. Holding the tenant row FOR UPDATE across that round-trip would
    # pin a scarce service-pool connection on network I/O (same reason the create
    # path dispatches its magic-link outside the tx). The status is re-checked
    # under the lock below to close the read-then-act race.
    precheck = await conn.fetchrow("SELECT status FROM tenants WHERE id = $1", tenant_id)
    if precheck is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    if precheck["status"] != "deleted":
        raise HTTPException(status_code=409, detail="tenant_must_be_soft_deleted_first")

    # Best-effort EA deprovision BEFORE the Postgres purge. EA is external — a
    # failed delete there must not block the purge, so deprovision_ea_account
    # never raises and we only log on failure. write_settings=False: the tenant
    # row is about to be purged, so don't waste an UPDATE marking its settings.
    ea_result = await deprovision_ea_account(conn, tenant_id=tenant_id, write_settings=False)
    if ea_result.failure_reason:
        logger.warning(
            "hard_delete_tenant: EA deprovision failed id=%s reason=%s",
            tenant_id,
            ea_result.failure_reason,
        )

    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT status, slug FROM tenants WHERE id = $1 FOR UPDATE",
            tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="tenant_not_found")
        if row["status"] != "deleted":
            raise HTTPException(
                status_code=409,
                detail="tenant_must_be_soft_deleted_first",
            )

        # All deletes happen inside the SECURITY DEFINER function so the
        # append-only audit_logs ceiling (migration 012) stays intact for
        # every other code path. The row is already locked FOR UPDATE on this
        # same connection, so the function's DELETE FROM tenants won't block.
        await conn.execute("SELECT admin_hard_delete_tenant($1)", tenant_id)

    logger.warning(
        "hard_delete_tenant: tenant permanently purged slug=%s id=%s by admin=%s",
        row["slug"],
        tenant_id,
        admin_id,
    )


def _mask_last4(token: str) -> str:
    """Return a masked preview exposing only the last 4 characters.

    Examples:
        _mask_last4("EAABsb-long-token-3a2f") -> "••••3a2f"
        _mask_last4("ab")                      -> "••••ab"   (shorter than 4, still safe)
    """
    suffix = token[-4:] if len(token) >= 4 else token
    return "••••" + suffix
