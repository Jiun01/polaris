"""Admin Demo System routes — Demo System v3 (ADR-066).

Mounted at ``/api/admin/demos``. Super-admin only (``require_platform_admin``);
service-pool (BYPASSRLS) so the operator sees every hidden demo tenant.

A "demo" is a hidden ``tenants`` row (``is_demo=true``) the operator owns, with
its own KB/persona/products and a text-gated ``demo_code``. Customers trigger it
by typing the code on the operator's live channels; the runtime
(``services/demo_runtime.py`` + ``pipeline.run_inbound``) does the roleplay.

Endpoints:
  * GET    /                       — list demos (excludes the polaris-internal gateway)
  * POST   /                       — create a demo (hidden tenant + quotas +
                                      non-login user for impersonation + audit)
  * GET    /{demo_id}              — detail
  * PATCH  /{demo_id}              — edit code / name / caps / description
  * DELETE /{demo_id}              — soft-delete + free the demo_code
  * POST   /{demo_id}/reset-visitor — clear a contact's one-demo-ever lock
  * GET    /{demo_id}/sessions     — session log + flagged leads

KB/persona/products are edited via the existing admin → portal impersonation
flow (no new editor); the non-login user created here supplies the
``primary_user_id`` impersonation needs.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from polaris_api.routers.admin_tenants import (
    _TIER_QUOTA_DEFAULTS,
    require_platform_admin,
)
from polaris_api.services import demo_sessions
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-demos"])

_FORBID = ConfigDict(extra="forbid")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$")
_GATEWAY = demo_sessions.POLARIS_INTERNAL_TENANT_ID
# Demos are content-only and excluded from billing/metrics; they use the
# enterprise quota defaults so KB/products are effectively unbounded for the
# demo (the real ceiling is the per-demo message cap on demo_sessions).
_DEMO_PLAN_TIER = "enterprise"


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return f"demo-{s}"[:48].strip("-")


class DemoCreate(BaseModel):
    model_config = _FORBID
    display_name: str = Field(min_length=1, max_length=120)
    demo_code: str = Field(min_length=3, max_length=64)
    duration_hours: int = Field(default=6, ge=1, le=720)
    max_messages: int = Field(default=50, ge=1, le=10000)
    slug: str | None = Field(default=None)
    description: str | None = Field(default=None, max_length=2000)


class DemoPatch(BaseModel):
    model_config = _FORBID
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    demo_code: str | None = Field(default=None, min_length=3, max_length=64)
    duration_hours: int | None = Field(default=None, ge=1, le=720)
    max_messages: int | None = Field(default=None, ge=1, le=10000)
    description: str | None = Field(default=None, max_length=2000)


class DemoSummary(BaseModel):
    id: str
    slug: str
    display_name: str
    demo_code: str | None
    duration_hours: int
    max_messages: int
    active_sessions: int
    created_at: str


class DemoCreateResponse(BaseModel):
    id: str
    slug: str


class DemoSessionRow(BaseModel):
    channel: str
    channel_user_id: str
    demo_code: str
    message_count: int
    max_messages: int
    is_expired: bool
    lead_flagged: bool
    activated_at: str
    expires_at: str


class ResetVisitorRequest(BaseModel):
    model_config = _FORBID
    channel_user_id: str = Field(min_length=1, max_length=128)


class ResetVisitorResponse(BaseModel):
    deleted: int


def _row_to_summary(row: asyncpg.Record) -> DemoSummary:
    return DemoSummary(
        id=str(row["id"]),
        slug=row["slug"],
        display_name=row["display_name"],
        demo_code=row["demo_code"],
        duration_hours=int(row["demo_duration_hours"]),
        max_messages=int(row["demo_max_messages"]),
        active_sessions=int(row["active_sessions"]),
        created_at=row["created_at"].isoformat(),
    )


@router.get("", response_model=list[DemoSummary])
async def list_demos(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> list[DemoSummary]:
    """All demo brands (excludes the polaris-internal gateway + soft-deleted)."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.slug, t.display_name, t.demo_code,
               t.demo_duration_hours, t.demo_max_messages, t.created_at,
               (SELECT COUNT(*) FROM demo_sessions ds
                 WHERE ds.tenant_id = t.id AND ds.is_expired = false) AS active_sessions
          FROM tenants t
         WHERE t.is_demo = true
           AND t.id <> $1
           AND t.status <> 'deleted'
         ORDER BY t.created_at DESC
        """,
        _GATEWAY,
    )
    return [_row_to_summary(r) for r in rows]


@router.post("/gateway/ensure", summary="Recreate the polaris-internal demo gateway if missing")
async def ensure_gateway(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """Idempotently restore the fixed-UUID demo gateway (row + persona from
    migrations 008b/105/114). The gateway is excluded from the demos list (it's
    the routing gateway, not a demo brand), so this is the only admin path to
    recover it after an accidental delete/overwrite. Mirrors the
    ``scripts/ensure_polaris_internal.py`` CLI."""
    result = await demo_sessions.ensure_polaris_internal_gateway(conn)
    await audit_record(
        conn,
        tenant_id=_GATEWAY,
        actor=admin_id,
        action="demo.gateway_ensured",
        after=result,
    )
    return {"ok": True, **result}


@router.post("", response_model=DemoCreateResponse, status_code=201)
async def create_demo(
    payload: DemoCreate,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> DemoCreateResponse:
    """Create a demo: hidden tenant (is_demo) + quotas + non-login user + audit."""
    slug = payload.slug or _slugify(payload.display_name)
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug_invalid_format")

    async with conn.transaction():
        if await conn.fetchval(
            "SELECT 1 FROM tenants WHERE slug = $1 AND status != 'deleted' LIMIT 1",
            slug,
        ):
            raise HTTPException(status_code=409, detail="slug_taken")
        if await conn.fetchval(
            "SELECT 1 FROM tenants WHERE lower(demo_code) = lower($1) "
            "AND status != 'deleted' LIMIT 1",
            payload.demo_code,
        ):
            raise HTTPException(status_code=409, detail="demo_code_taken")

        settings = {"company_name": payload.display_name}
        if payload.description:
            settings["description"] = payload.description

        tenant_id = await conn.fetchval(
            """
            INSERT INTO tenants
              (slug, display_name, status, plan_tier, is_demo,
               demo_code, demo_duration_hours, demo_max_messages, settings)
            VALUES ($1, $2, 'active', $3, true, $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            slug,
            payload.display_name,
            _DEMO_PLAN_TIER,
            payload.demo_code,
            payload.duration_hours,
            payload.max_messages,
            settings,
        )

        # tenant_quotas — reuse the enterprise tier defaults so impersonation +
        # KB/product management behave like a real tenant.
        q = _TIER_QUOTA_DEFAULTS[_DEMO_PLAN_TIER]
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
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16
            )
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            _DEMO_PLAN_TIER,
            q["bot_replies"],
            q["conversation_retention_days"],
            q["allowed_channel_kinds"],
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

        # Non-login user so admin impersonation has a primary_user_id. The email
        # is synthetic + non-routable — demo tenants never log in.
        user_id = await conn.fetchval(
            "SELECT public.upsert_auth_user_by_email($1)",
            f"demo+{slug}@demo.polaris.invalid",
        )
        await conn.execute(
            """
            INSERT INTO tenant_users (tenant_id, user_id, role)
            VALUES ($1, $2, 'admin')
            ON CONFLICT (tenant_id, user_id) DO NOTHING
            """,
            tenant_id,
            user_id,
        )

        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="demo.created",
            after={
                "slug": slug,
                "demo_code": payload.demo_code,
                "duration_hours": payload.duration_hours,
                "max_messages": payload.max_messages,
            },
        )

    logger.info("admin_demos: created demo slug=%s code=%s", slug, payload.demo_code)
    return DemoCreateResponse(id=str(tenant_id), slug=slug)


async def _fetch_demo(conn: asyncpg.Connection, demo_id: UUID) -> asyncpg.Record:
    row = await conn.fetchrow(
        """
        SELECT t.id, t.slug, t.display_name, t.demo_code,
               t.demo_duration_hours, t.demo_max_messages, t.created_at,
               (SELECT COUNT(*) FROM demo_sessions ds
                 WHERE ds.tenant_id = t.id AND ds.is_expired = false) AS active_sessions
          FROM tenants t
         WHERE t.id = $1 AND t.is_demo = true AND t.id <> $2
           AND t.status <> 'deleted'
        """,
        demo_id,
        _GATEWAY,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="DEMO_NOT_FOUND")
    return row


@router.get("/{demo_id}", response_model=DemoSummary)
async def get_demo(
    demo_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> DemoSummary:
    return _row_to_summary(await _fetch_demo(conn, demo_id))


@router.patch("/{demo_id}", response_model=DemoSummary)
async def update_demo(
    demo_id: UUID,
    payload: DemoPatch,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> DemoSummary:
    async with conn.transaction():
        await _fetch_demo(conn, demo_id)  # 404 if missing
        if payload.demo_code is not None and await conn.fetchval(
            "SELECT 1 FROM tenants WHERE lower(demo_code) = lower($1) "
            "AND id <> $2 AND status != 'deleted' LIMIT 1",
            payload.demo_code,
            demo_id,
        ):
            raise HTTPException(status_code=409, detail="demo_code_taken")

        sets: list[str] = []
        args: list[Any] = []
        col_map = {
            "display_name": payload.display_name,
            "demo_code": payload.demo_code,
            "demo_duration_hours": payload.duration_hours,
            "demo_max_messages": payload.max_messages,
        }
        for col, val in col_map.items():
            if val is not None:
                args.append(val)
                sets.append(f"{col} = ${len(args)}")
        if payload.description is not None:
            args.append({"description": payload.description})
            sets.append(f"settings = settings || ${len(args)}::jsonb")
        if sets:
            args.append(demo_id)
            await conn.execute(
                f"UPDATE tenants SET {', '.join(sets)} WHERE id = ${len(args)}",  # noqa: S608
                *args,
            )
        await audit_record(
            conn,
            tenant_id=demo_id,
            actor=admin_id,
            action="demo.updated",
            after={k: v for k, v in col_map.items() if v is not None},
        )
        return _row_to_summary(await _fetch_demo(conn, demo_id))


@router.delete("/{demo_id}", status_code=204)
async def delete_demo(
    demo_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> None:
    """Soft-delete the demo and free its demo_code for reuse.

    Sets deleted_at + purge_at to satisfy the ``tenants_deleted_state_complete``
    CHECK (migration 013); a retention sweep physically purges later.
    """
    async with conn.transaction():
        await _fetch_demo(conn, demo_id)  # 404 if missing
        await conn.execute(
            """
            UPDATE tenants
               SET status = 'deleted',
                   deleted_at = now(),
                   purge_at = now() + INTERVAL '30 days',
                   demo_code = NULL
             WHERE id = $1
            """,
            demo_id,
        )
        await audit_record(
            conn,
            tenant_id=demo_id,
            actor=admin_id,
            action="demo.deleted",
            after={"status": "deleted"},
        )


@router.post("/{demo_id}/reset-visitor", response_model=ResetVisitorResponse)
async def reset_visitor(
    demo_id: UUID,
    payload: ResetVisitorRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> ResetVisitorResponse:
    """Clear a contact's one-demo-ever lock so they can demo again."""
    await _fetch_demo(conn, demo_id)  # 404 if missing
    deleted = await demo_sessions.reset_visitor_demo(payload.channel_user_id)
    return ResetVisitorResponse(deleted=deleted)


@router.get("/{demo_id}/sessions", response_model=list[DemoSessionRow])
async def list_sessions(
    demo_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> list[DemoSessionRow]:
    """Session log + flagged leads for this demo."""
    await _fetch_demo(conn, demo_id)  # 404 if missing
    rows = await conn.fetch(
        """
        SELECT channel, channel_user_id, demo_code, message_count, max_messages,
               is_expired, lead_flagged_at, activated_at, expires_at
          FROM demo_sessions
         WHERE tenant_id = $1
         ORDER BY activated_at DESC
         LIMIT 500
        """,
        demo_id,
    )
    return [
        DemoSessionRow(
            channel=r["channel"],
            channel_user_id=r["channel_user_id"],
            demo_code=r["demo_code"],
            message_count=r["message_count"],
            max_messages=r["max_messages"],
            is_expired=r["is_expired"],
            lead_flagged=r["lead_flagged_at"] is not None,
            activated_at=r["activated_at"].isoformat(),
            expires_at=r["expires_at"].isoformat(),
        )
        for r in rows
    ]
