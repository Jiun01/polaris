"""Internal E2E seed/teardown endpoints — Phase 9 VPS-mimic tests.

Mounted at ``/internal/e2e`` by main.py ONLY when
``POLARIS_E2E_INTERNAL_SECRET`` is set in the environment.  Every
endpoint requires that same secret in the ``Authorization: Bearer …``
header.  If the secret is empty or missing the router is never registered.

Endpoints
---------
POST   /internal/e2e/tenants                  — create synthetic tenant
DELETE /internal/e2e/tenants/{tenant_id}      — hard-delete synthetic tenant
POST   /internal/e2e/mrr-rollup               — seed MRR total to exact value
POST   /internal/e2e/plan-change-requests               — create pending PCR
POST   /internal/e2e/plan-change-requests/{id}/decide  — force PCR to approved/rejected
POST   /internal/e2e/audit-chain-corrupt               — corrupt a row_hash
POST   /internal/e2e/bookings                 — seed a booking row (Phase 13)
POST   /internal/e2e/booking-reminder-tick    — force the reminder worker tick (Phase 13)
POST   /internal/e2e/demo-inbound             — simulate a demo inbound (Phase 14a)
POST   /internal/e2e/demo-session-query       — query demo_sessions by channel_user_id (Phase 14a)
POST   /internal/e2e/demo-session-set-count   — force message_count for expiry-cap tests (Phase 14a)
POST   /internal/e2e/demo-session-teardown    — hard-delete demo session rows (Phase 14a teardown)
POST   /internal/e2e/audience-query           — query audience_members by channel_user_id (Phase 14b)
POST   /internal/e2e/audit-query              — query audit_logs rows by tenant_id (Phase 20)
POST   /internal/e2e/bookings-query           — query bookings rows by tenant_id (Phase 20)
POST   /internal/e2e/booking-reminder-set-due — set booking_reminders.scheduled_at (Phase 13 quiet-hours)
POST   /internal/e2e/booking-reminder-query   — query booking_reminders by booking_id (Phase 13)
POST   /internal/e2e/conversations-query      — query conversations.handoff_at by tenant+CW id (Phase 10+)
GET    /internal/e2e/meta-outbound-query      — return meta-mock captured calls (Phase 10+ E2E)
DELETE /internal/e2e/meta-outbound-clear      — clear meta-mock captured calls (Phase 10+ E2E)
POST   /internal/e2e/broadcast-e2e-seed       — seed template + broadcast + recipients for WA tenant (Phase 20)
POST   /internal/e2e/broadcast-e2e-tick       — DB-driven drain for due broadcasts (Phase 20, bypasses Redis)
POST   /internal/e2e/broadcast-recipients-query — query broadcast_recipients by broadcast_id (Phase 20)

Security
--------
- Router is never mounted if the env var is unset → zero attack surface in prod.
- All DB writes target synthetic rows (slug LIKE 'e2e-%' guard) except the
  MRR sentinel which zeros tenant_billing.mrr_myr platform-wide — acceptable
  because this router is only mounted in local VPS-mimic / CI environments
  that have no real billing data.
- auth.users deletes are restricted to emails ending ``@polaris-e2e.invalid``.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from polaris_api.services.admin.pool import get_admin_service_conn
from polaris_api.services.demo_sessions import POLARIS_INTERNAL_TENANT_ID

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal-e2e"])

_FORBID = ConfigDict(extra="forbid")

# ---------------------------------------------------------------------------
# Auth guard — identical secret in every endpoint header
# ---------------------------------------------------------------------------

# Read once at module import.  The router is only registered if non-empty,
# so if we somehow end up here with an empty secret we still block.
def _get_secret() -> str:
    """Return the E2E secret from the environment (lazy to pick up test monkeypatches)."""
    import os
    return os.environ.get("POLARIS_E2E_INTERNAL_SECRET", "")


def _require_e2e_auth(request: Request) -> None:
    """FastAPI dependency — raises 403 when the bearer token is wrong."""
    secret = _get_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="E2E endpoints not active")

    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    if not (token and hmac.compare_digest(token, secret)):
        raise HTTPException(status_code=403, detail="Forbidden")


_Auth = Annotated[None, Depends(_require_e2e_auth)]
_Conn = Annotated[asyncpg.Connection, Depends(get_admin_service_conn)]


# ---------------------------------------------------------------------------
# POST /internal/e2e/tenants
# ---------------------------------------------------------------------------

class _SeedTenantBody(BaseModel):
    model_config = _FORBID
    slug: str | None = None
    tier: str = "starter"


class _SeedTenantOut(BaseModel):
    model_config = _FORBID
    tenant_id: str
    tenant_slug: str
    tenant_admin_email: str
    tenant_admin_user_id: str


@router.post(
    "/tenants",
    response_model=_SeedTenantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Seed a synthetic active tenant for E2E tests",
)
async def seed_tenant(
    body: _SeedTenantBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    slug = body.slug or f"e2e-{int(time.time() * 1000)}"
    tier = body.tier if body.tier in ("starter", "pro", "ultra", "enterprise") else "starter"
    email = f"e2e-admin-{slug}@polaris-e2e.invalid"

    async with conn.transaction():
        tenant_id = await conn.fetchval(
            """
            INSERT INTO tenants (slug, display_name, status, plan_tier, settings)
            VALUES ($1, $2, 'active', $3, '{}'::jsonb)
            RETURNING id
            """,
            slug,
            f"E2E Tenant {slug}",
            tier,
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
                bot_replies_period_cap
            ) VALUES (
                $1, $2,
                10000, 90,
                ARRAY['whatsapp'], false,
                false, 100,
                60, 1000000,
                1000000, 1000,
                10000, 1000,
                10000
            )
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            tier,
        )

        await conn.execute(
            """
            INSERT INTO tenant_billing (tenant_id, status, plan_tier, billing_contact_email)
            VALUES ($1, 'active', $2, $3)
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            tier,
            email,
        )

        user_id = await conn.fetchval(
            "SELECT public.upsert_auth_user_by_email($1)",
            email,
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

        # One audit_log row so corruptAuditHash has something to target.
        # Must use a correctly computed row_hash so verify_chain reports intact.
        from datetime import UTC
        from datetime import datetime as _dt

        from polaris_api.services.audit import compute_row_hash as _crh
        _created_at = _dt.now(UTC)
        row_hash = _crh(
            prev_hash=None,
            tenant_id=tenant_id,
            actor=None,
            action="tenant.created",
            before=None,
            after={"seed": True},
            created_at=_created_at,
        )
        await conn.execute(
            """
            INSERT INTO audit_logs
                   (tenant_id, actor, action, before, after, prev_hash, row_hash, created_at)
            VALUES ($1, NULL, 'tenant.created', NULL, '{"seed":true}'::jsonb, NULL, $2, $3)
            """,
            tenant_id,
            row_hash,
            _created_at,
        )

    logger.info("[internal-e2e] seedTenant slug=%s tier=%s", slug, tier)
    return {
        "tenant_id": str(tenant_id),
        "tenant_slug": slug,
        "tenant_admin_email": email,
        "tenant_admin_user_id": str(user_id),
    }


# ---------------------------------------------------------------------------
# DELETE /internal/e2e/tenants/{tenant_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/tenants/{tenant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete a synthetic tenant created by seedTenant",
)
async def teardown_tenant(
    tenant_id: UUID,
    conn: _Conn,
    _auth: _Auth,
) -> None:
    # Gateway guard (defense-in-depth): never let the E2E teardown delete the
    # polaris-internal demo gateway. The delete runs via the SECURITY DEFINER
    # delete_e2e_tenant_noncascade_rows() below, which won't self-check — so
    # this app-level early return, BEFORE conn.transaction(), is the only guard.
    if tenant_id == POLARIS_INTERNAL_TENANT_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="gateway_protected: refusing to delete the polaris-internal demo gateway",
        )
    async with conn.transaction():
        # Capture seed user_ids before cascaded delete removes tenant_users rows.
        user_rows = await conn.fetch(
            "SELECT user_id FROM tenant_users WHERE tenant_id = $1",
            tenant_id,
        )

        # Delete non-cascade child rows via SECURITY DEFINER function
        # (polaris_service lacks DELETE on audit_logs — append-only per ADR-030).
        await conn.execute(
            "SELECT public.delete_e2e_tenant_noncascade_rows($1)",
            tenant_id,
        )

        deleted = await conn.fetchval(
            "DELETE FROM tenants WHERE id = $1 RETURNING id",
            tenant_id,
        )
        if deleted is None:
            raise HTTPException(status_code=404, detail="Tenant not found")

        # Hard-delete auth.users rows created by this seed (E2E-only guard).
        for row in user_rows:
            await conn.execute(
                "SELECT public.delete_e2e_auth_user($1)",
                row["user_id"],
            )

    logger.info("[internal-e2e] teardownTenant tenant_id=%s", tenant_id)


# ---------------------------------------------------------------------------
# POST /internal/e2e/mrr-rollup
# ---------------------------------------------------------------------------

class _SeedMrrBody(BaseModel):
    model_config = _FORBID
    mrr_myr: float


class _SeedMrrOut(BaseModel):
    model_config = _FORBID
    mrr_myr: float
    alert_band: str


@router.post(
    "/mrr-rollup",
    response_model=_SeedMrrOut,
    status_code=status.HTTP_200_OK,
    summary="Force MRR total to an exact value for tripwire E2E tests",
)
async def seed_mrr(
    body: _SeedMrrBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    from polaris_api.services.mrr_rollup import _compute_band  # noqa: PLC2701

    async with conn.transaction():
        # Zero ALL tenant billing so the sentinel is the only contributor.
        await conn.execute("UPDATE tenant_billing SET mrr_myr = 0")

        sentinel_slug = "e2e-mrr-sentinel"
        sentinel_email = "e2e-mrr-sentinel@polaris-e2e.invalid"

        tenant_id = await conn.fetchval(
            "SELECT id FROM tenants WHERE slug = $1 LIMIT 1",
            sentinel_slug,
        )

        if tenant_id is None:
            tenant_id = await conn.fetchval(
                """
                INSERT INTO tenants (slug, display_name, status, plan_tier, settings)
                VALUES ($1, 'E2E MRR Sentinel', 'active', 'starter', '{}'::jsonb)
                RETURNING id
                """,
                sentinel_slug,
            )
            await conn.fetchval(
                "SELECT public.upsert_auth_user_by_email($1)",
                sentinel_email,
            )
            await conn.execute(
                """
                INSERT INTO tenant_billing (tenant_id, status, plan_tier, billing_contact_email, mrr_myr)
                VALUES ($1, 'active', 'starter', $2, $3)
                ON CONFLICT (tenant_id) DO UPDATE SET mrr_myr = EXCLUDED.mrr_myr
                """,
                tenant_id,
                sentinel_email,
                body.mrr_myr,
            )
        else:
            await conn.execute(
                "UPDATE tenant_billing SET mrr_myr = $1 WHERE tenant_id = $2",
                body.mrr_myr,
                tenant_id,
            )

    band = _compute_band(body.mrr_myr)
    logger.info("[internal-e2e] seedMrr mrr_myr=%.2f band=%s", body.mrr_myr, band)
    return {"mrr_myr": body.mrr_myr, "alert_band": band}


# ---------------------------------------------------------------------------
# POST /internal/e2e/plan-change-requests
# ---------------------------------------------------------------------------

class _SeedPcrBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    target_tier: str


class _SeedPcrOut(BaseModel):
    model_config = _FORBID
    request_id: str


@router.post(
    "/plan-change-requests",
    response_model=_SeedPcrOut,
    status_code=status.HTTP_201_CREATED,
    summary="Seed a pending plan-change request for a synthetic tenant",
)
async def seed_plan_change_request(
    body: _SeedPcrBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    target_tier = (
        body.target_tier
        if body.target_tier in ("starter", "pro", "ultra", "enterprise")
        else "pro"
    )

    # Fetch the tenant's current tier and a user_id to act as requester.
    tenant_row = await conn.fetchrow(
        "SELECT plan_tier FROM tenants WHERE id = $1 LIMIT 1",
        body.tenant_id,
    )
    if tenant_row is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    user_row = await conn.fetchrow(
        "SELECT user_id FROM tenant_users WHERE tenant_id = $1 LIMIT 1",
        body.tenant_id,
    )
    if user_row is None:
        raise HTTPException(status_code=404, detail="No tenant_user found")

    request_id = await conn.fetchval(
        """
        INSERT INTO plan_change_requests (
            tenant_id, requested_by_user_id,
            current_tier, target_tier,
            current_billing_cycle, target_billing_cycle,
            status
        ) VALUES ($1, $2, $3, $4, 'monthly', 'monthly', 'pending')
        RETURNING id
        """,
        body.tenant_id,
        user_row["user_id"],
        tenant_row["plan_tier"],
        target_tier,
    )

    logger.info("[internal-e2e] seedPcr tenant=%s request=%s", body.tenant_id, request_id)
    return {"request_id": str(request_id)}


# ---------------------------------------------------------------------------
# POST /internal/e2e/plan-change-requests/{request_id}/decide
# ---------------------------------------------------------------------------

class _DecidePcrBody(BaseModel):
    model_config = _FORBID
    decision: str  # "approved" or "rejected"
    decision_note: str = "e2e-decided"


class _DecidePcrOut(BaseModel):
    model_config = _FORBID
    request_id: str
    status: str


@router.post(
    "/plan-change-requests/{request_id}/decide",
    response_model=_DecidePcrOut,
    summary="Force a PCR to approved or rejected status (E2E only)",
)
async def decide_plan_change_request(
    request_id: UUID,
    body: _DecidePcrBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    from datetime import UTC
    from datetime import datetime as _dt
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")
    decided_at = _dt.now(UTC)
    updated = await conn.fetchval(
        "UPDATE plan_change_requests "
        "SET status = $1, decided_at = $2, decision_note = $3 "
        "WHERE id = $4 RETURNING id",
        body.decision,
        decided_at,
        body.decision_note,
        request_id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="plan_change_not_found")
    logger.info("[internal-e2e] decidePcr request=%s decision=%s", request_id, body.decision)
    return {"request_id": str(request_id), "status": body.decision}


# ---------------------------------------------------------------------------
# POST /internal/e2e/audit-chain-corrupt
# ---------------------------------------------------------------------------

class _CorruptAuditBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID


@router.post(
    "/audit-chain-corrupt",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Corrupt the most-recent audit_log row_hash for a tenant",
)
async def corrupt_audit_hash(
    body: _CorruptAuditBody,
    conn: _Conn,
    _auth: _Auth,
) -> None:
    # polaris_service lacks UPDATE on audit_logs (append-only per ADR-030 /
    # migration 012). Delegate to the SECURITY DEFINER helper from migration 065
    # which runs as the owner and has the required privilege.
    updated_id = await conn.fetchval(
        "SELECT public.corrupt_e2e_audit_hash($1)",
        body.tenant_id,
    )
    if updated_id is None:
        raise HTTPException(
            status_code=404,
            detail="No audit_log rows found for this tenant",
        )
    logger.info("[internal-e2e] corruptAuditHash tenant=%s row=%s", body.tenant_id, updated_id)


# ---------------------------------------------------------------------------
# POST /internal/e2e/bookings  (Phase 13)
# ---------------------------------------------------------------------------

class _SeedBookingBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    customer_handle: str
    start_at: str           # ISO-8601 UTC, e.g. "2026-06-05T09:00:00Z"
    channel: str = "whatsapp"
    service_name: str | None = None
    appointment_id: str | None = None
    status: str = "confirmed"
    # v2.1 link-mode binding (booking-link.journey E2E). `binding_token` is the
    # opaque `jti` stored in bookings.binding_token — the EA webhook resolves a
    # pending booking by (tenant_id, jti). Pass the same jti you sign into the
    # `polaris_ref` token. Only meaningful for status="pending".
    binding_token: str | None = None
    binding_ttl_seconds: int = 600


class _SeedBookingOut(BaseModel):
    model_config = _FORBID
    booking_id: str
    reminder_id: str | None


@router.post(
    "/bookings",
    response_model=_SeedBookingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Seed a booking row + schedule its reminder (Phase 13 E2E)",
)
async def seed_booking(
    body: _SeedBookingBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    from datetime import datetime

    allowed_statuses = ("pending", "confirmed", "cancelled", "rescheduled")
    bk_status = body.status if body.status in allowed_statuses else "confirmed"

    allowed_channels = ("whatsapp", "instagram", "messenger", "instagram_login")
    channel = body.channel if body.channel in allowed_channels else "whatsapp"

    try:
        start_at = datetime.fromisoformat(body.start_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid start_at: {exc}") from exc

    from datetime import UTC, timedelta
    binding_expires_at = (
        datetime.now(UTC) + timedelta(seconds=body.binding_ttl_seconds)
        if body.binding_token
        else None
    )

    async with conn.transaction():
        booking_id = await conn.fetchval(
            """
            INSERT INTO bookings
                (tenant_id, customer_handle, channel, start_at,
                 service_name, appointment_id, status,
                 binding_token, binding_expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::booking_status, $8, $9)
            RETURNING id
            """,
            body.tenant_id,
            body.customer_handle,
            channel,
            start_at,
            body.service_name,
            body.appointment_id,
            bk_status,
            body.binding_token,
            binding_expires_at,
        )

    # Schedule a reminder only for confirmed bookings.
    reminder_id_str: str | None = None
    if bk_status == "confirmed":
        from polaris_api.services.booking.config import read_booking_config
        from polaris_api.services.booking_reminders import schedule_reminder

        # Read tenant's configured offset (default 24h) via the shared helper
        # so this mirrors the production confirm path (link_flow).
        booking_config = await read_booking_config(conn, body.tenant_id)
        offset_hours = int(booking_config.get("reminder_offset_hours", 24))

        reminder_id = await schedule_reminder(
            conn,
            booking_id=booking_id,
            tenant_id=body.tenant_id,
            start_at=start_at,
            offset_hours=offset_hours,
        )
        reminder_id_str = str(reminder_id)

    logger.info(
        "[internal-e2e] seedBooking tenant=%s booking=%s status=%s reminder=%s",
        body.tenant_id,
        booking_id,
        bk_status,
        reminder_id_str,
    )
    return {"booking_id": str(booking_id), "reminder_id": reminder_id_str}


# ---------------------------------------------------------------------------
# POST /internal/e2e/booking-reminder-tick  (Phase 13)
# ---------------------------------------------------------------------------

class _ReminderTickOut(BaseModel):
    model_config = _FORBID
    sent_count: int


@router.post(
    "/booking-reminder-tick",
    response_model=_ReminderTickOut,
    status_code=status.HTTP_200_OK,
    summary="Force the booking reminder worker to process all due reminders (Phase 13 E2E)",
)
async def booking_reminder_tick(
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    from polaris_api.services.booking_reminders import tick_due
    from polaris_api.services.meta_whatsapp import get_meta_whatsapp_client

    meta_client = get_meta_whatsapp_client()
    sent_count = await tick_due(conn, meta_client=meta_client)
    logger.info("[internal-e2e] bookingReminderTick sent=%d", sent_count)
    return {"sent_count": sent_count}


# ---------------------------------------------------------------------------
# POST /internal/e2e/demo-inbound  (Phase 14a)
# ---------------------------------------------------------------------------

class _DemoInboundBody(BaseModel):
    model_config = _FORBID
    channel_user_id: str
    channel_user_name: str | None = None
    demo_code: str = "phase-2-default"


class _DemoInboundOut(BaseModel):
    model_config = _FORBID
    session_id: str
    message_count: int
    created: bool


@router.post(
    "/demo-inbound",
    response_model=_DemoInboundOut,
    status_code=status.HTTP_200_OK,
    summary="Simulate a demo inbound message — calls upsert_session directly (Phase 14a E2E)",
)
async def seed_demo_inbound(
    body: _DemoInboundBody,
    _auth: _Auth,
) -> dict[str, Any]:
    # Demo System v3 (ADR-066): resolve the code -> hidden brand tenant, then
    # activate (or increment an existing active session). Mirrors demo_runtime.
    from fastapi import HTTPException

    from polaris_api.services import demo_sessions as ds

    active = await ds.get_active_demo_session(body.channel_user_id)
    if active is not None:
        new_count = await ds.increment_demo_message(active.session_id)
        return {
            "session_id": str(active.session_id),
            "message_count": new_count,
            "created": False,
        }

    demo = await ds.find_demo_tenant_by_code(body.demo_code)
    if demo is None:
        raise HTTPException(status_code=404, detail="demo_code_not_found")
    if await ds.has_ever_used_demo(body.channel_user_id):
        raise HTTPException(status_code=409, detail="demo_already_used")

    session = await ds.activate_demo_session(
        channel="whatsapp",
        channel_user_id=body.channel_user_id,
        channel_user_name=body.channel_user_name,
        demo=demo,
    )
    logger.info(
        "[internal-e2e] demoInbound channel_user_id=%s code=%s activated",
        body.channel_user_id,
        body.demo_code,
    )
    return {
        "session_id": str(session.session_id),
        "message_count": session.message_count,
        "created": True,
    }


# ---------------------------------------------------------------------------
# POST /internal/e2e/demo-session-query  (Phase 14a)
# ---------------------------------------------------------------------------

class _DemoSessionQueryBody(BaseModel):
    model_config = _FORBID
    channel_user_id: str


class _DemoSessionQueryOut(BaseModel):
    model_config = _FORBID
    found: bool
    session_id: str | None = None
    tenant_id: str | None = None
    message_count: int | None = None
    max_messages: int | None = None
    is_expired: bool | None = None
    expires_at: str | None = None
    auto_purge_at: str | None = None
    demo_code: str | None = None
    activated_at: str | None = None


@router.post(
    "/demo-session-query",
    response_model=_DemoSessionQueryOut,
    status_code=status.HTTP_200_OK,
    summary="Query demo_sessions by channel_user_id (most recent row) (Phase 14a E2E)",
)
async def demo_session_query(
    body: _DemoSessionQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT id, tenant_id, message_count, max_messages, is_expired,
               expires_at, auto_purge_at, demo_code, activated_at
          FROM demo_sessions
         WHERE channel_user_id = $1
         ORDER BY activated_at DESC
         LIMIT 1
        """,
        body.channel_user_id,
    )
    if row is None:
        return {"found": False}
    return {
        "found": True,
        "session_id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "message_count": row["message_count"],
        "max_messages": row["max_messages"],
        "is_expired": row["is_expired"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "auto_purge_at": row["auto_purge_at"].isoformat() if row["auto_purge_at"] else None,
        "demo_code": row["demo_code"],
        "activated_at": row["activated_at"].isoformat() if row["activated_at"] else None,
    }


# ---------------------------------------------------------------------------
# POST /internal/e2e/demo-session-set-count  (Phase 14a)
# ---------------------------------------------------------------------------

class _DemoSetCountBody(BaseModel):
    model_config = _FORBID
    channel_user_id: str
    message_count: int


class _DemoSetCountOut(BaseModel):
    model_config = _FORBID
    updated: bool


@router.post(
    "/demo-session-set-count",
    response_model=_DemoSetCountOut,
    status_code=status.HTTP_200_OK,
    summary="Force message_count on active demo session — for expiry-cap E2E tests (Phase 14a)",
)
async def demo_session_set_count(
    body: _DemoSetCountBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    result = await conn.execute(
        """
        UPDATE demo_sessions
           SET message_count = $2
         WHERE channel_user_id = $1
           AND is_expired = false
        """,
        body.channel_user_id,
        body.message_count,
    )
    count = int(result.split()[-1]) if result else 0
    logger.info(
        "[internal-e2e] demoSessionSetCount channel_user_id=%s count=%d updated=%d",
        body.channel_user_id,
        body.message_count,
        count,
    )
    return {"updated": count > 0}


# ---------------------------------------------------------------------------
# POST /internal/e2e/demo-session-teardown  (Phase 14a)
# ---------------------------------------------------------------------------

class _DemoTeardownBody(BaseModel):
    model_config = _FORBID
    channel_user_id: str


class _DemoTeardownOut(BaseModel):
    model_config = _FORBID
    deleted: int


@router.post(
    "/demo-session-teardown",
    response_model=_DemoTeardownOut,
    status_code=status.HTTP_200_OK,
    summary="Hard-delete all demo_sessions rows for a channel_user_id (Phase 14a teardown)",
)
async def demo_session_teardown(
    body: _DemoTeardownBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    result = await conn.execute(
        "DELETE FROM demo_sessions WHERE channel_user_id = $1",
        body.channel_user_id,
    )
    count = int(result.split()[-1]) if result else 0
    logger.info(
        "[internal-e2e] demoSessionTeardown channel_user_id=%s deleted=%d",
        body.channel_user_id,
        count,
    )
    return {"deleted": count}


# ---------------------------------------------------------------------------
# POST /internal/e2e/audience-query  (Phase 14b)
# ---------------------------------------------------------------------------

class _AudienceQueryBody(BaseModel):
    model_config = _FORBID
    channel_user_id: str


class _AudienceQueryRow(BaseModel):
    model_config = _FORBID
    audience_member_id: str
    tenant_id: str
    channel: str
    channel_user_id: str
    channel_user_name: str | None
    phone_number_e164: str | None
    metadata: dict[str, Any] | None
    tags: list[Any] | None
    opt_in_source: str | None
    opted_in_at: str | None
    opted_out_at: str | None = None


class _AudienceQueryOut(BaseModel):
    model_config = _FORBID
    found: bool
    rows: list[_AudienceQueryRow]


@router.post(
    "/audience-query",
    response_model=_AudienceQueryOut,
    status_code=status.HTTP_200_OK,
    summary="Query audience_members by channel_user_id (all tenants, BYPASSRLS) (Phase 14b E2E)",
)
async def audience_query(
    body: _AudienceQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    import json as _json

    rows = await conn.fetch(
        """
        SELECT id, tenant_id, channel, channel_user_id, channel_user_name,
               phone_number_e164, metadata, tags, opt_in_source, opted_in_at,
               opted_out_at
          FROM audience_members
         WHERE channel_user_id = $1
         ORDER BY opted_in_at DESC NULLS LAST
         LIMIT 20
        """,
        body.channel_user_id,
    )
    result_rows = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except _json.JSONDecodeError:
                meta = None
        tags = r["tags"]
        if isinstance(tags, str):
            try:
                tags = _json.loads(tags)
            except _json.JSONDecodeError:
                tags = None
        result_rows.append({
            "audience_member_id": str(r["id"]),
            "tenant_id": str(r["tenant_id"]),
            "channel": str(r["channel"]),
            "channel_user_id": str(r["channel_user_id"]),
            "channel_user_name": r["channel_user_name"],
            "phone_number_e164": r["phone_number_e164"],
            "metadata": meta,
            "tags": tags if isinstance(tags, list) else (list(tags) if tags else None),
            "opt_in_source": r["opt_in_source"],
            "opted_in_at": r["opted_in_at"].isoformat() if r["opted_in_at"] else None,
            "opted_out_at": r["opted_out_at"].isoformat() if r["opted_out_at"] else None,
        })
    logger.info(
        "[internal-e2e] audienceQuery channel_user_id=%s rows=%d",
        body.channel_user_id,
        len(result_rows),
    )
    return {"found": len(result_rows) > 0, "rows": result_rows}


# ---------------------------------------------------------------------------
# POST /internal/e2e/audit-query  (Phase 20)
# ---------------------------------------------------------------------------

class _AuditQueryBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    limit: int = 50


class _AuditQueryRow(BaseModel):
    model_config = _FORBID
    id: str
    action: str
    actor: str | None
    created_at: str
    after: dict[str, Any] | None = None


class _AuditQueryOut(BaseModel):
    model_config = _FORBID
    row_count: int
    rows: list[_AuditQueryRow]


@router.post(
    "/audit-query",
    response_model=_AuditQueryOut,
    status_code=status.HTTP_200_OK,
    summary="Query audit_logs rows by tenant_id (BYPASSRLS) (Phase 20 E2E)",
)
async def audit_query(
    body: _AuditQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    import json as _json

    limit = max(1, min(body.limit, 500))
    rows = await conn.fetch(
        """
        SELECT id, action, actor, created_at, after
          FROM audit_logs
         WHERE tenant_id = $1
         ORDER BY created_at DESC
         LIMIT $2
        """,
        body.tenant_id,
        limit,
    )
    result_rows = []
    for r in rows:
        after = r["after"]
        if isinstance(after, str):
            try:
                after = _json.loads(after)
            except _json.JSONDecodeError:
                after = None
        result_rows.append({
            "id": str(r["id"]),
            "action": r["action"],
            "actor": str(r["actor"]) if r["actor"] else None,
            "created_at": r["created_at"].isoformat(),
            "after": after,
        })
    logger.info(
        "[internal-e2e] auditQuery tenant_id=%s rows=%d",
        body.tenant_id,
        len(result_rows),
    )
    return {"row_count": len(result_rows), "rows": result_rows}


# ---------------------------------------------------------------------------
# POST /internal/e2e/bookings-query  (Phase 20)
# ---------------------------------------------------------------------------

class _BookingsQueryBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID


class _BookingsQueryRow(BaseModel):
    model_config = _FORBID
    booking_id: str
    customer_handle: str
    channel: str
    start_at: str
    status: str


class _BookingsQueryOut(BaseModel):
    model_config = _FORBID
    row_count: int
    rows: list[_BookingsQueryRow]


@router.post(
    "/bookings-query",
    response_model=_BookingsQueryOut,
    status_code=status.HTTP_200_OK,
    summary="Query bookings rows by tenant_id (BYPASSRLS) (Phase 20 E2E)",
)
async def bookings_query(
    body: _BookingsQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT id, customer_handle, channel, start_at, status
          FROM bookings
         WHERE tenant_id = $1
         ORDER BY created_at DESC
         LIMIT 50
        """,
        body.tenant_id,
    )
    result_rows = [
        {
            "booking_id": str(r["id"]),
            "customer_handle": r["customer_handle"],
            "channel": r["channel"],
            "start_at": r["start_at"].isoformat(),
            "status": str(r["status"]),
        }
        for r in rows
    ]
    logger.info(
        "[internal-e2e] bookingsQuery tenant_id=%s rows=%d",
        body.tenant_id,
        len(result_rows),
    )
    return {"row_count": len(result_rows), "rows": result_rows}


# ---------------------------------------------------------------------------
# POST /internal/e2e/booking-reminder-set-due  (Phase 13 quiet-hours E2E)
# ---------------------------------------------------------------------------

class _ReminderSetDueBody(BaseModel):
    model_config = _FORBID
    booking_id: UUID
    scheduled_at: str  # ISO-8601 UTC — sets the reminder due time


class _ReminderSetDueOut(BaseModel):
    model_config = _FORBID
    updated: bool
    reminder_id: str | None = None


@router.post(
    "/booking-reminder-set-due",
    response_model=_ReminderSetDueOut,
    status_code=status.HTTP_200_OK,
    summary="Override scheduled_at on an unsent booking reminder (Phase 13 quiet-hours E2E)",
)
async def booking_reminder_set_due(
    body: _ReminderSetDueBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    from datetime import datetime

    try:
        scheduled_at = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid scheduled_at: {exc}") from exc

    row = await conn.fetchrow(
        """
        UPDATE booking_reminders
           SET scheduled_at = $2, updated_at = now()
         WHERE booking_id = $1
           AND sent_at IS NULL
         RETURNING id
        """,
        body.booking_id,
        scheduled_at,
    )
    updated = row is not None
    logger.info(
        "[internal-e2e] reminderSetDue booking_id=%s updated=%s",
        body.booking_id,
        updated,
    )
    return {"updated": updated, "reminder_id": str(row["id"]) if row else None}


# ---------------------------------------------------------------------------
# POST /internal/e2e/booking-reminder-query  (Phase 13)
# ---------------------------------------------------------------------------

class _ReminderQueryBody(BaseModel):
    model_config = _FORBID
    booking_id: UUID


class _ReminderQueryRow(BaseModel):
    model_config = _FORBID
    reminder_id: str
    scheduled_at: str
    sent_at: str | None
    attempts: int
    last_error: str | None


class _ReminderQueryOut(BaseModel):
    model_config = _FORBID
    found: bool
    rows: list[_ReminderQueryRow]


@router.post(
    "/booking-reminder-query",
    response_model=_ReminderQueryOut,
    status_code=status.HTTP_200_OK,
    summary="Query booking_reminders by booking_id (Phase 13 E2E)",
)
async def booking_reminder_query(
    body: _ReminderQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT id, scheduled_at, sent_at, attempts, last_error
          FROM booking_reminders
         WHERE booking_id = $1
         ORDER BY created_at DESC
        """,
        body.booking_id,
    )
    result_rows = [
        {
            "reminder_id": str(r["id"]),
            "scheduled_at": r["scheduled_at"].isoformat(),
            "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
            "attempts": r["attempts"],
            "last_error": r["last_error"],
        }
        for r in rows
    ]
    logger.info(
        "[internal-e2e] reminderQuery booking_id=%s rows=%d",
        body.booking_id,
        len(result_rows),
    )
    return {"found": len(result_rows) > 0, "rows": result_rows}


# ---------------------------------------------------------------------------
# POST /internal/e2e/conversations-query  (Phase 10+ reverse-handoff E2E)
# ---------------------------------------------------------------------------

class _ConversationsQueryBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    chatwoot_conversation_id: int | None = None


class _ConversationsQueryRow(BaseModel):
    model_config = _FORBID
    conversation_id: str
    chatwoot_conversation_id: int | None
    handoff_at: str | None
    handoff_pending_recovery_at: str | None


class _ConversationsQueryOut(BaseModel):
    model_config = _FORBID
    found: bool
    rows: list[_ConversationsQueryRow]


@router.post(
    "/conversations-query",
    response_model=_ConversationsQueryOut,
    status_code=status.HTTP_200_OK,
    summary="Query conversations.handoff_at by tenant_id (BYPASSRLS) (Phase 10+ E2E)",
)
async def conversations_query(
    body: _ConversationsQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    if body.chatwoot_conversation_id is not None:
        rows = await conn.fetch(
            """
            SELECT id, chatwoot_conversation_id, handoff_at, handoff_pending_recovery_at
              FROM conversations
             WHERE tenant_id = $1
               AND chatwoot_conversation_id = $2
             ORDER BY created_at DESC
             LIMIT 10
            """,
            body.tenant_id,
            body.chatwoot_conversation_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, chatwoot_conversation_id, handoff_at, handoff_pending_recovery_at
              FROM conversations
             WHERE tenant_id = $1
             ORDER BY created_at DESC
             LIMIT 20
            """,
            body.tenant_id,
        )
    result_rows = [
        {
            "conversation_id": str(r["id"]),
            "chatwoot_conversation_id": r["chatwoot_conversation_id"],
            "handoff_at": r["handoff_at"].isoformat() if r["handoff_at"] else None,
            "handoff_pending_recovery_at": (
                r["handoff_pending_recovery_at"].isoformat()
                if r["handoff_pending_recovery_at"]
                else None
            ),
        }
        for r in rows
    ]
    logger.info(
        "[internal-e2e] conversationsQuery tenant_id=%s cw_conv=%s rows=%d",
        body.tenant_id,
        body.chatwoot_conversation_id,
        len(result_rows),
    )
    return {"found": len(result_rows) > 0, "rows": result_rows}


# ─── /meta-outbound-query + /meta-outbound-clear ─────────────────────────────


@router.get(
    "/meta-outbound-query",
    summary="Return all calls captured by the Meta Graph API mock",
    description=(
        "Only meaningful when POLARIS_META_MOCK_ENABLED=true and "
        "META_API_BASE_URL points to /meta-mock. Returns the list of "
        "captured POST payloads in arrival order."
    ),
)
async def meta_outbound_query(_auth: _Auth) -> dict[str, Any]:
    from polaris_api.services.meta_mock_store import get_all  # noqa: PLC0415

    captures = await get_all()
    return {"count": len(captures), "captures": captures}


@router.delete(
    "/meta-outbound-clear",
    summary="Clear the Meta Graph API mock capture store",
    description=(
        "Resets the in-memory capture list to empty. "
        "Use before each test that asserts outbound counts."
    ),
    status_code=200,
)
async def meta_outbound_clear(_auth: _Auth) -> dict[str, Any]:
    from polaris_api.services.meta_mock_store import clear  # noqa: PLC0415

    n = await clear()
    logger.info("[internal-e2e] meta-outbound-clear: cleared %d captures", n)
    return {"cleared": n}


# ---------------------------------------------------------------------------
# POST /internal/e2e/broadcast-e2e-seed  (Phase 20 broadcast E2E)
# ---------------------------------------------------------------------------

class _BroadcastSeedBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    template_name: str = "e2e_broadcast_test"


class _BroadcastSeedOut(BaseModel):
    model_config = _FORBID
    broadcast_id: str
    template_id: str
    channel_id: str
    recipient_count: int


@router.post(
    "/broadcast-e2e-seed",
    response_model=_BroadcastSeedOut,
    status_code=201,
    summary=(
        "Seed approved template + scheduled broadcast + recipients for a tenant's WA channel "
        "(Phase 20 E2E — pairs with broadcast-e2e-tick)"
    ),
)
async def broadcast_e2e_seed(
    body: _BroadcastSeedBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    from polaris_api.services.secrets import put as _secrets_put  # noqa: PLC0415

    channel_row = await conn.fetchrow(
        """
        SELECT id, phone_number_id
          FROM tenant_channels
         WHERE tenant_id = $1 AND channel = 'whatsapp'
         LIMIT 1
        """,
        body.tenant_id,
    )
    if channel_row is None:
        raise HTTPException(status_code=404, detail="No WhatsApp channel found for this tenant")
    channel_id: UUID = channel_row["id"]

    async with conn.transaction():
        # Clear wa_message_ids from prior E2E broadcasts so the meta-mock's
        # sequential IDs (wamid.E2EMock0001, ...) don't hit the unique index.
        await conn.execute(
            "UPDATE broadcast_recipients SET wa_message_id = NULL WHERE tenant_id = $1",
            body.tenant_id,
        )

        # Ensure the WABA token exists so the worker can call Meta mock.
        await _secrets_put(conn, body.tenant_id, "meta_waba_access_token", b"e2e_mock_token")

        # Upsert an approved template for this channel.
        template_id: UUID = await conn.fetchval(
            """
            INSERT INTO tenant_templates
                (tenant_id, tenant_channel_id, name, language, category, status, components)
            VALUES ($1, $2, $3, 'en_US', 'UTILITY', 'approved',
                    '[{"type":"BODY","text":"Hello, this is a test broadcast."}]'::jsonb)
            ON CONFLICT (tenant_id, tenant_channel_id, name, language)
            DO UPDATE SET status = 'approved', updated_at = now()
            RETURNING id
            """,
            body.tenant_id,
            channel_id,
            body.template_name,
        )

        # Create a broadcast already past its scheduled_at so the tick picks it up.
        broadcast_id: UUID = await conn.fetchval(
            """
            INSERT INTO broadcasts
                (tenant_id, tenant_channel_id, template_id, name,
                 audience_filter, status, scheduled_at)
            VALUES ($1, $2, $3, 'E2E Broadcast Test',
                    '{"filter_kind":"all_opted_in"}'::jsonb,
                    'scheduled', now() - interval '1 second')
            RETURNING id
            """,
            body.tenant_id,
            channel_id,
            template_id,
        )

        # Collect opted-in WA audience members for this tenant.
        audience_rows = await conn.fetch(
            """
            SELECT id, channel_user_id
              FROM audience_members
             WHERE tenant_id = $1
               AND channel = 'whatsapp'
               AND opted_out_at IS NULL
             LIMIT 100
            """,
            body.tenant_id,
        )

        if audience_rows:
            await conn.executemany(
                """
                INSERT INTO broadcast_recipients
                    (broadcast_id, tenant_id, audience_member_id, channel, channel_user_id)
                VALUES ($1, $2, $3, 'whatsapp', $4)
                """,
                [
                    (broadcast_id, body.tenant_id, r["id"], r["channel_user_id"])
                    for r in audience_rows
                ],
            )
            await conn.execute(
                "UPDATE broadcasts SET total_recipients = $1 WHERE id = $2",
                len(audience_rows),
                broadcast_id,
            )

    recipient_count = len(audience_rows)
    logger.info(
        "[internal-e2e] broadcastE2eSeed tenant=%s broadcast=%s template=%s recipients=%d",
        body.tenant_id,
        broadcast_id,
        body.template_name,
        recipient_count,
    )
    return {
        "broadcast_id": str(broadcast_id),
        "template_id": str(template_id),
        "channel_id": str(channel_id),
        "recipient_count": recipient_count,
    }


# ---------------------------------------------------------------------------
# POST /internal/e2e/broadcast-e2e-tick  (Phase 20 broadcast E2E)
# ---------------------------------------------------------------------------

class _BroadcastTickBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID


class _BroadcastTickOut(BaseModel):
    model_config = _FORBID
    drained_count: int


@router.post(
    "/broadcast-e2e-tick",
    response_model=_BroadcastTickOut,
    status_code=200,
    summary=(
        "DB-driven broadcast drain — processes all scheduled+due broadcasts for a tenant "
        "(Phase 20 E2E — bypasses Redis ZSET, calls _drain_broadcast directly)"
    ),
)
async def broadcast_e2e_tick(
    body: _BroadcastTickBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    from polaris_api.services.broadcast.worker import _drain_broadcast  # noqa: PLC0415, PLC2701

    due_rows = await conn.fetch(
        """
        SELECT id
          FROM broadcasts
         WHERE tenant_id = $1
           AND status = 'scheduled'
           AND scheduled_at <= now()
         ORDER BY scheduled_at ASC
         LIMIT 20
        """,
        body.tenant_id,
    )

    drained = 0
    for row in due_rows:
        try:
            await _drain_broadcast(tenant_id=body.tenant_id, broadcast_id=row["id"])
            drained += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "[internal-e2e] broadcastE2eTick: drain failed broadcast=%s", row["id"]
            )

    logger.info(
        "[internal-e2e] broadcastE2eTick tenant=%s due=%d drained=%d",
        body.tenant_id,
        len(due_rows),
        drained,
    )
    return {"drained_count": drained}


# ---------------------------------------------------------------------------
# POST /internal/e2e/broadcast-recipients-query  (Phase 20 broadcast E2E)
# ---------------------------------------------------------------------------

class _BroadcastRecipientsQueryBody(BaseModel):
    model_config = _FORBID
    broadcast_id: UUID


class _BroadcastRecipientsRow(BaseModel):
    model_config = _FORBID
    recipient_id: str
    channel_user_id: str
    status: str
    wa_message_id: str | None
    sent_at: str | None
    skip_reason: str | None


class _BroadcastRecipientsOut(BaseModel):
    model_config = _FORBID
    found: bool
    rows: list[_BroadcastRecipientsRow]


@router.post(
    "/broadcast-recipients-query",
    response_model=_BroadcastRecipientsOut,
    status_code=200,
    summary="Query broadcast_recipients by broadcast_id (BYPASSRLS) (Phase 20 E2E)",
)
async def broadcast_recipients_query(
    body: _BroadcastRecipientsQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT id, channel_user_id, status, wa_message_id, sent_at, skip_reason
          FROM broadcast_recipients
         WHERE broadcast_id = $1
         ORDER BY created_at ASC
        """,
        body.broadcast_id,
    )
    result_rows = [
        {
            "recipient_id": str(r["id"]),
            "channel_user_id": r["channel_user_id"],
            "status": str(r["status"]),
            "wa_message_id": r["wa_message_id"],
            "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
            "skip_reason": r["skip_reason"],
        }
        for r in rows
    ]
    logger.info(
        "[internal-e2e] broadcastRecipientsQuery broadcast_id=%s rows=%d",
        body.broadcast_id,
        len(result_rows),
    )
    return {"found": len(result_rows) > 0, "rows": result_rows}


# ---------------------------------------------------------------------------
# POST /internal/e2e/mint-tenant-jwt  (RLS / token-separation E2E)
# ---------------------------------------------------------------------------

class _MintTenantJwtBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    user_id: UUID | None = None   # defaults to a seeded tenant_user of this tenant
    role: str = "admin"
    ttl_seconds: int = 900        # pass a negative value to mint an expired token
    issuer: str | None = None     # override `iss` to test wrong-issuer → 401


class _MintTenantJwtOut(BaseModel):
    model_config = _FORBID
    token: str
    tenant_id: str
    user_id: str


@router.post(
    "/mint-tenant-jwt",
    response_model=_MintTenantJwtOut,
    status_code=status.HTTP_200_OK,
    summary="Mint a portal tenant JWT (HS256) for RLS / token-separation E2E tests",
)
async def mint_tenant_jwt(
    body: _MintTenantJwtBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    import jwt as _jwt

    from polaris_api.config import get_settings

    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="server misconfigured: jwt_secret unset")

    user_id = body.user_id
    if user_id is None:
        row = await conn.fetchrow(
            "SELECT user_id FROM tenant_users WHERE tenant_id = $1 LIMIT 1",
            body.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="No tenant_user found for tenant")
        user_id = row["user_id"]

    now = int(time.time())
    payload: dict[str, Any] = {
        "tenant_id": str(body.tenant_id),
        "sub": str(user_id),
        "role": body.role,
        "iss": body.issuer or settings.jwt_issuer,
        "iat": now,
        "exp": now + body.ttl_seconds,
    }
    token = _jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    logger.info(
        "[internal-e2e] mintTenantJwt tenant=%s user=%s ttl=%d iss=%s",
        body.tenant_id,
        user_id,
        body.ttl_seconds,
        payload["iss"],
    )
    return {"token": token, "tenant_id": str(body.tenant_id), "user_id": str(user_id)}


# ---------------------------------------------------------------------------
# POST /internal/e2e/mint-admin-jwt  (token-separation E2E)
# ---------------------------------------------------------------------------

class _MintAdminJwtBody(BaseModel):
    model_config = _FORBID
    platform_admin_id: UUID | None = None  # defaults to a seeded platform_admins row
    role: str = "platform_admin"
    ttl_seconds: int = 900


class _MintAdminJwtOut(BaseModel):
    model_config = _FORBID
    token: str
    platform_admin_id: str


@router.post(
    "/mint-admin-jwt",
    response_model=_MintAdminJwtOut,
    status_code=status.HTTP_200_OK,
    summary="Mint a platform_admin JWT (HS256) for token-separation E2E tests",
)
async def mint_admin_jwt(
    body: _MintAdminJwtBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    """Mint a faithful platform_admin JWT.

    Admin tokens are signed with the SAME ``jwt_secret``/issuer as tenant
    tokens (see ``admin_tenants._decode_admin_jwt``); the only difference is
    ``role=platform_admin`` and the ABSENCE of a ``tenant_id`` claim. That is
    exactly why such a token is accepted on ``/api/admin/*`` but rejected with
    401 on ``/api/tenant/*`` (the tenant dep requires a ``tenant_id`` claim).
    """
    import jwt as _jwt

    from polaris_api.config import get_settings

    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="server misconfigured: jwt_secret unset")

    admin_id = body.platform_admin_id
    if admin_id is None:
        row = await conn.fetchrow("SELECT id FROM platform_admins LIMIT 1")
        if row is None:
            raise HTTPException(status_code=404, detail="No platform_admins row found")
        admin_id = row["id"]

    now = int(time.time())
    payload: dict[str, Any] = {
        # Deliberately NO tenant_id — that is what makes this a platform token.
        "sub": str(admin_id),
        "platform_admin_id": str(admin_id),
        "role": body.role,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + body.ttl_seconds,
    }
    token = _jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    logger.info("[internal-e2e] mintAdminJwt admin=%s role=%s ttl=%d", admin_id, body.role, body.ttl_seconds)
    return {"token": token, "platform_admin_id": str(admin_id)}


# ---------------------------------------------------------------------------
# POST /internal/e2e/branches-query  (branch→EA-provider isolation E2E)
# ---------------------------------------------------------------------------

class _BranchesQueryBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID


class _BranchesQueryRow(BaseModel):
    model_config = _FORBID
    branch_id: str
    slug: str | None
    name: str | None
    ea_provider_id: int | None
    ea_default_service_id: int | None


class _BranchesQueryOut(BaseModel):
    model_config = _FORBID
    row_count: int
    rows: list[_BranchesQueryRow]


@router.post(
    "/branches-query",
    response_model=_BranchesQueryOut,
    status_code=status.HTTP_200_OK,
    summary="Query tenant_branches EA bindings by tenant_id (BYPASSRLS) (branch-routing E2E)",
)
async def branches_query(
    body: _BranchesQueryBody,
    conn: _Conn,
    _auth: _Auth,
) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT id, slug, name, ea_provider_id, ea_default_service_id
          FROM tenant_branches
         WHERE tenant_id = $1
         ORDER BY slug NULLS LAST, name
        """,
        body.tenant_id,
    )
    result_rows = [
        {
            "branch_id": str(r["id"]),
            "slug": r["slug"],
            "name": r["name"],
            "ea_provider_id": r["ea_provider_id"],
            "ea_default_service_id": r["ea_default_service_id"],
        }
        for r in rows
    ]
    logger.info(
        "[internal-e2e] branchesQuery tenant_id=%s rows=%d",
        body.tenant_id,
        len(result_rows),
    )
    return {"row_count": len(result_rows), "rows": result_rows}


# ---------------------------------------------------------------------------
# POST /internal/e2e/quota-set  (quota soft-warn / hard-cap E2E)
# ---------------------------------------------------------------------------

class _QuotaSetBody(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    count: int  # value to write into the current-period bot_replies counter


class _QuotaSetOut(BaseModel):
    model_config = _FORBID
    key: str
    count: int


@router.post(
    "/quota-set",
    response_model=_QuotaSetOut,
    status_code=status.HTTP_200_OK,
    summary="Force the Redis bot_replies period counter for a tenant (quota E2E)",
)
async def quota_set(body: _QuotaSetBody, _auth: _Auth) -> dict[str, Any]:
    from datetime import UTC, datetime

    from polaris_api.redis_pool import get_redis

    key = f"bot_replies:{body.tenant_id}:{datetime.now(UTC).strftime('%Y%m')}"
    await get_redis().set(key, body.count)
    logger.info("[internal-e2e] quotaSet tenant=%s key=%s count=%d", body.tenant_id, key, body.count)
    return {"key": key, "count": body.count}
