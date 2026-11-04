"""Admin sales-quotes routes — Phase 9 PR-9.

Mounted at ``/api/admin/quotes`` (registered in main.py).

Endpoints:
  * ``POST /``               — generate a prospect quote PDF
  * ``GET  /``               — list quotes with optional filters
  * ``GET  /{id}/pdf``       — 307 redirect to signed PDF URL
  * ``POST /{id}/convert/{tenant_id}`` — convert quote to a tenant

Schema (migration 054):
  sales_quotes columns:
    id, quote_code, prospect_email, prospect_company,
    target_tier, target_billing_cycle, intended_signup_date,
    proration_inputs (JSONB), pdf_bucket, pdf_object_key,
    generated_by_admin_id, converted_to_tenant_id, converted_at,
    expires_at, created_at

Auth: ``require_platform_admin``.
ADR: ADR-028 (quote format POL-Q-YYMMINDEX), ADR-030 (audit every write).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from polaris_api.config import get_settings
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record
from polaris_api.services.billing.invoice_sequencer import next_quote_number
from polaris_api.services.billing.pdf_renderer import render_quote_pdf
from polaris_api.services.billing.proration import (
    BillingCycle,
    PlanTier,
    ProrationInputs,
    compute,
)
from polaris_api.services.billing.storage import get_signed_url, upload_pdf

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-quotes"])
_FORBID = ConfigDict(extra="forbid")
_TZ_MYT = ZoneInfo("Asia/Kuala_Lumpur")

_SELECT_COLS = (
    "id, quote_code, prospect_email, prospect_company, "
    "target_tier, target_billing_cycle, intended_signup_date, "
    "proration_inputs, pdf_bucket, pdf_object_key, "
    "generated_by_admin_id, converted_to_tenant_id, converted_at, "
    "expires_at, created_at"
)


class _CreateQuoteRequest(BaseModel):
    model_config = _FORBID
    prospect_email: str | None = Field(default=None, max_length=254)
    prospect_company: str | None = Field(default=None, max_length=120)
    target_tier: str = Field(..., description="starter | pro | ultra | enterprise")
    target_billing_cycle: str = Field(..., description="monthly | annual")
    intended_signup_date: date
    valid_days: int = Field(default=30, ge=1, le=365)


class _SalesQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    quote_code: str
    prospect_email: str | None = None
    prospect_company: str | None = None
    target_tier: str
    target_billing_cycle: str
    intended_signup_date: date
    expires_at: datetime
    converted_to_tenant_id: UUID | None = None
    converted_at: datetime | None = None
    created_at: datetime


class _ListQuotesResponse(BaseModel):
    model_config = _FORBID
    items: list[_SalesQuote]
    total: int
    offset: int
    limit: int


def _row_to_quote(row: asyncpg.Record) -> _SalesQuote:
    return _SalesQuote(
        id=row["id"],
        quote_code=row["quote_code"],
        prospect_email=row["prospect_email"],
        prospect_company=row["prospect_company"],
        target_tier=row["target_tier"],
        target_billing_cycle=row["target_billing_cycle"],
        intended_signup_date=row["intended_signup_date"],
        expires_at=row["expires_at"],
        converted_to_tenant_id=row["converted_to_tenant_id"],
        converted_at=row["converted_at"],
        created_at=row["created_at"],
    )


@router.post(
    "/",
    response_model=_SalesQuote,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a prospect sales quote PDF",
)
async def create_quote(
    body: _CreateQuoteRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _SalesQuote:
    """Generate a sales quote: proration compute → PDF → Storage → DB + audit."""
    try:
        target_tier = PlanTier(body.target_tier)
        billing_cycle = BillingCycle(body.target_billing_cycle)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid target_tier or target_billing_cycle: {exc}",
        ) from exc

    today = body.intended_signup_date
    period_start = today.replace(day=1)

    proration = compute(
        ProrationInputs(
            target_tier=target_tier,
            billing_cycle=billing_cycle,
            current_tier=None,
            period_start=period_start,
            today=today,
        )
    )

    settings = get_settings()
    quote_code = await next_quote_number(conn)

    proration_inputs: dict[str, Any] = {
        "headline_price_myr": str(proration.headline_price_myr),
        "headline_reply_cap": proration.headline_reply_cap,
        "amount_due_myr": str(proration.amount_due_myr),
        "quota_topup_bot_replies": proration.quota_topup_bot_replies,
        "ratio": str(proration.ratio),
        "days_billed": proration.days_billed,
        "days_in_period": proration.days_in_period,
        "renderer_version": "1",
    }

    pdf_context: dict[str, Any] = {
        "quote_code": quote_code,
        "prospect_email": body.prospect_email or "",
        "prospect_company": body.prospect_company or "",
        "target_tier": target_tier.value,
        "target_billing_cycle": billing_cycle.value,
        "amount_due_myr": f"{proration.amount_due_myr:.2f}",
        "headline_price_myr": f"{proration.headline_price_myr:.2f}",
        "headline_reply_cap": proration.headline_reply_cap,
        "quota_topup_bot_replies": proration.quota_topup_bot_replies,
        "proration_ratio": str(proration.ratio),
        "days_billed": proration.days_billed,
        "days_in_period": proration.days_in_period,
        "intended_signup_date": today.isoformat(),
        "issued_date": datetime.now(_TZ_MYT).strftime("%Y-%m-%d"),
    }

    try:
        pdf_bytes = await render_quote_pdf(pdf_context)
    except Exception as exc:
        logger.error("Quote PDF render failed quote=%s err=%s", quote_code, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="pdf_render_failed",
        ) from exc

    quote_id = uuid4()
    bucket = settings.supabase_storage_quotes_bucket
    object_key = f"quotes/{quote_id}.pdf"

    try:
        await upload_pdf(bucket, object_key, pdf_bytes, settings)
    except Exception as exc:
        logger.error("Quote PDF upload failed quote=%s err=%s", quote_code, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="storage_upload_failed",
        ) from exc

    expires_at = datetime.now(UTC) + timedelta(days=body.valid_days)

    async with conn.transaction():
        row = await conn.fetchrow(
            f"""
            INSERT INTO sales_quotes
              (id, quote_code, prospect_email, prospect_company,
               target_tier, target_billing_cycle, intended_signup_date,
               proration_inputs, pdf_bucket, pdf_object_key,
               generated_by_admin_id, expires_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12)
            RETURNING {_SELECT_COLS}
            """,  # noqa: S608 — _SELECT_COLS is a hardcoded constant, not user input
            quote_id,
            quote_code,
            body.prospect_email,
            body.prospect_company,
            target_tier.value,
            billing_cycle.value,
            today,
            # dict DIRECTLY — JSONB codec serialises once (services/audit.py).
            proration_inputs,
            bucket,
            object_key,
            admin_id,
            expires_at,
        )
        assert row is not None  # noqa: S101
        # quote.created has no tenant context (prospect quote pre-conversion);
        # audit is recorded at convert time instead.

    logger.info(
        "quote.created code=%s actor=%s",
        quote_code,
        admin_id,
    )
    return _row_to_quote(row)


@router.get(
    "/",
    response_model=_ListQuotesResponse,
    summary="List sales quotes",
)
async def list_quotes(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    converted: bool | None = Query(default=None, description="True=converted only, False=unconverted only"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> _ListQuotesResponse:
    """List sales quotes, optionally filtered by conversion status."""
    conditions: list[str] = []
    params: list[Any] = []
    p = 1

    if converted is True:
        conditions.append("converted_to_tenant_id IS NOT NULL")
    elif converted is False:
        conditions.append("converted_to_tenant_id IS NULL")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM sales_quotes {where}",  # noqa: S608
        *params,
    ) or 0
    rows = await conn.fetch(
        f"SELECT {_SELECT_COLS} FROM sales_quotes {where} "  # noqa: S608
        f"ORDER BY created_at DESC LIMIT ${p} OFFSET ${p + 1}",
        *list(params) + [limit, offset],
    )
    return _ListQuotesResponse(
        items=[_row_to_quote(r) for r in rows],
        total=int(total),
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{quote_id}/pdf",
    summary="Redirect to signed quote PDF URL",
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
)
async def get_quote_pdf(
    quote_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> Response:
    """Fetch a signed Supabase Storage URL and 307-redirect to it."""
    row = await conn.fetchrow(
        "SELECT pdf_bucket, pdf_object_key FROM sales_quotes WHERE id = $1",
        quote_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="quote_not_found")
    if not row["pdf_object_key"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pdf_not_available")
    settings = get_settings()
    try:
        signed_url = await get_signed_url(row["pdf_bucket"], row["pdf_object_key"], settings)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="storage_signed_url_failed",
        ) from exc
    return Response(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": signed_url},
    )


@router.post(
    "/{quote_id}/convert/{tenant_id}",
    response_model=_SalesQuote,
    summary="Mark a quote as converted to a specific tenant",
)
async def convert_quote(
    quote_id: UUID,
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _SalesQuote:
    """Mark a quote as converted; link it to the given tenant.

    Also flips the tenant's plan_tier to the quoted target_tier.
    """
    row = await conn.fetchrow(
        f"SELECT {_SELECT_COLS} FROM sales_quotes WHERE id = $1",  # noqa: S608
        quote_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="quote_not_found")
    if row["converted_to_tenant_id"] is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="quote_already_converted",
        )

    today_dt = datetime.now(UTC)
    if row["expires_at"] < today_dt:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="quote_expired",
        )

    # Verify tenant exists.
    tenant_row = await conn.fetchrow(
        "SELECT id, plan_tier FROM tenants WHERE id = $1 LIMIT 1",
        tenant_id,
    )
    if tenant_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found")

    converted_at = today_dt
    async with conn.transaction():
        await conn.execute(
            "UPDATE sales_quotes "
            "SET converted_to_tenant_id = $1, converted_at = $2 "
            "WHERE id = $3",
            tenant_id,
            converted_at,
            quote_id,
        )
        await conn.execute(
            "UPDATE tenants SET plan_tier = $1 WHERE id = $2",
            row["target_tier"],
            tenant_id,
        )
        await audit_record(
            conn,
            tenant_id=tenant_id,
            actor=admin_id,
            action="quote.converted",
            before={"plan_tier": tenant_row["plan_tier"]},
            after={
                "plan_tier": row["target_tier"],
                "quote_code": row["quote_code"],
                "converted_at": converted_at.isoformat(),
            },
        )

    logger.info(
        "quote.converted id=%s tenant=%s actor=%s",
        quote_id,
        tenant_id,
        admin_id,
    )
    updated = await conn.fetchrow(
        f"SELECT {_SELECT_COLS} FROM sales_quotes WHERE id = $1",  # noqa: S608
        quote_id,
    )
    assert updated is not None  # noqa: S101
    return _row_to_quote(updated)
