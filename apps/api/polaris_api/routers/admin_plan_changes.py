from __future__ import annotations

import json as _json
import logging
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from polaris_api.config import get_settings
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record
from polaris_api.services.billing.invoice_sequencer import next_invoice_number
from polaris_api.services.billing.pdf_renderer import render_invoice_pdf
from polaris_api.services.billing.proration import (
    BillingCycle,
    PlanTier,
    ProrationInputs,
    compute,
)
from polaris_api.services.billing.storage import upload_pdf

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-plan-changes"])
_FORBID = ConfigDict(extra="forbid")
_TZ_MYT = ZoneInfo("Asia/Kuala_Lumpur")

_SELECT_COLS = (
    "pcr.id, pcr.tenant_id, t.display_name AS tenant_name, "
    "pcr.requested_by_user_id, pcr.current_tier, pcr.target_tier, "
    "pcr.current_billing_cycle, pcr.target_billing_cycle, "
    "pcr.proration_preview, pcr.status, pcr.reason, "
    "pcr.decided_by_user_id, pcr.decided_at, pcr.decision_note, "
    "pcr.created_at"
)


class _PlanChangeRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    tenant_id: UUID
    tenant_name: str | None = None
    requested_by_user_id: UUID | None = None
    current_tier: str
    target_tier: str
    current_billing_cycle: str
    target_billing_cycle: str
    proration_preview: dict[str, Any] | None = None
    status: str
    reason: str | None = None
    decided_by_user_id: UUID | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime


class _ListPlanChangesResponse(BaseModel):
    model_config = _FORBID
    items: list[_PlanChangeRow]
    total: int
    offset: int
    limit: int


class _RejectBody(BaseModel):
    model_config = _FORBID
    decision_note: str = Field(..., min_length=1, max_length=2000)


class _ApproveResponse(BaseModel):
    model_config = _FORBID
    request_id: UUID
    status: str
    invoice_id: UUID
    invoice_number: str
    amount_due_myr: float
    approved_at: str | None = None
    idempotent: bool = False


class _RejectResponse(BaseModel):
    model_config = _FORBID
    request_id: UUID
    status: str
    decided_at: str


def _row_to_dto(row: asyncpg.Record) -> _PlanChangeRow:
    preview = row["proration_preview"]
    if isinstance(preview, str):
        preview = _json.loads(preview)
    return _PlanChangeRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        tenant_name=row["tenant_name"],
        requested_by_user_id=row["requested_by_user_id"],
        current_tier=row["current_tier"],
        target_tier=row["target_tier"],
        current_billing_cycle=row["current_billing_cycle"],
        target_billing_cycle=row["target_billing_cycle"],
        proration_preview=preview,
        status=row["status"],
        reason=row["reason"],
        decided_by_user_id=row["decided_by_user_id"],
        decided_at=row["decided_at"],
        decision_note=row["decision_note"],
        created_at=row["created_at"],
    )


@router.get(
    "/",
    response_model=_ListPlanChangesResponse,
    summary="List plan-change requests (default: pending only)",
)
async def list_plan_changes(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    status_filter: str | None = Query(default="pending", alias="status"),
    tenant_id: Annotated[UUID | None, Query()] = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> _ListPlanChangesResponse:
    conditions: list[str] = []
    params: list[Any] = []
    p = 1
    if status_filter:
        conditions.append(f"pcr.status = ${p}")
        params.append(status_filter)
        p += 1
    if tenant_id:
        conditions.append(f"pcr.tenant_id = ${p}")
        params.append(tenant_id)
        p += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    count_sql = (
        f"SELECT COUNT(*) FROM plan_change_requests pcr "  # noqa: S608
        f"LEFT JOIN tenants t ON t.id = pcr.tenant_id {where}"
    )
    total = await conn.fetchval(count_sql, *params) or 0
    rows_sql = (
        f"SELECT {_SELECT_COLS} FROM plan_change_requests pcr "  # noqa: S608
        f"LEFT JOIN tenants t ON t.id = pcr.tenant_id {where} "
        f"ORDER BY pcr.created_at DESC LIMIT ${p} OFFSET ${p + 1}"
    )
    rows = await conn.fetch(rows_sql, *list(params) + [limit, offset])
    return _ListPlanChangesResponse(
        items=[_row_to_dto(r) for r in rows],
        total=int(total),
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{request_id}",
    response_model=_PlanChangeRow,
    summary="Fetch one plan-change request",
)
async def get_plan_change(
    request_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _PlanChangeRow:
    row = await conn.fetchrow(
        f"SELECT {_SELECT_COLS} FROM plan_change_requests pcr "  # noqa: S608
        "LEFT JOIN tenants t ON t.id = pcr.tenant_id "
        "WHERE pcr.id = $1",
        request_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan_change_not_found")
    return _row_to_dto(row)


@router.post(
    "/{request_id}/approve",
    response_model=_ApproveResponse,
    summary="Approve a plan-change request (server-authoritative proration)",
)
async def approve_plan_change(
    request_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _ApproveResponse:
    req_row = await conn.fetchrow(
        f"SELECT {_SELECT_COLS} FROM plan_change_requests pcr "  # noqa: S608
        "LEFT JOIN tenants t ON t.id = pcr.tenant_id "
        "WHERE pcr.id = $1",
        request_id,
    )
    if req_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan_change_not_found")
    if req_row["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot_approve_status_" + req_row["status"],
        )

    existing_invoice = await conn.fetchrow(
        "SELECT id, invoice_number, amount_myr FROM tenant_invoices "
        "WHERE proration_basis->>'plan_change_request_id' = $1 LIMIT 1",
        str(request_id),
    )
    if existing_invoice is not None:
        logger.info("plan_change.approve idempotent request_id=%s invoice=%s", request_id, existing_invoice["invoice_number"])
        return _ApproveResponse(
            request_id=request_id,
            status="approved",
            invoice_id=existing_invoice["id"],
            invoice_number=existing_invoice["invoice_number"],
            amount_due_myr=float(existing_invoice["amount_myr"]),
            idempotent=True,
        )

    today = datetime.now(_TZ_MYT).date()
    period_start = today.replace(day=1)
    try:
        current_tier = PlanTier(req_row["current_tier"])
        target_tier = PlanTier(req_row["target_tier"])
        billing_cycle = BillingCycle(req_row["target_billing_cycle"])
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"invalid_tier_or_cycle: {exc}") from exc

    proration = compute(
        ProrationInputs(
            target_tier=target_tier,
            billing_cycle=billing_cycle,
            current_tier=current_tier,
            period_start=period_start,
            today=today,
        )
    )

    settings = get_settings()
    billing_cycle_str = billing_cycle.value
    invoice_number = await next_invoice_number(conn, billing_cycle_str)
    tenant_name: str = req_row["tenant_name"] or str(req_row["tenant_id"])
    import calendar
    days_in_month = calendar.monthrange(period_start.year, period_start.month)[1]
    period_end = date(period_start.year, period_start.month, days_in_month)
    # period column is 'YYYY-MM' for monthly, 'YYYY' for annual (migration 008)
    period_str = (
        f"{period_start.year}-{period_start.month:02d}"
        if billing_cycle_str == "monthly"
        else str(period_start.year)
    )

    pdf_context: dict[str, Any] = {
        "invoice_number": invoice_number,
        "invoice_kind": "plan_change_proration",
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "subtotal_myr": f"{proration.amount_due_myr:.2f}",
        "tax_myr": "0.00",
        "total_myr": f"{proration.amount_due_myr:.2f}",
        "tenant_name": tenant_name,
        "issued_date": today.isoformat(),
        "due_date": today.isoformat(),
        "current_tier": current_tier.value,
        "target_tier": target_tier.value,
        "proration_ratio": str(proration.ratio),
        "days_billed": proration.days_billed,
        "days_in_period": proration.days_in_period,
    }

    import os as _os
    _e2e_mode = bool(_os.environ.get("POLARIS_E2E_INTERNAL_SECRET", ""))

    invoice_id = uuid4()
    tid = str(req_row["tenant_id"])
    object_key = "invoices/" + tid + "/" + str(invoice_id) + ".pdf"

    if _e2e_mode:
        # Skip PDF rendering and storage upload in E2E mode — WeasyPrint
        # system deps are absent in the dev container.
        logger.info("plan_change.approve E2E mode: skipping PDF render/upload request_id=%s", request_id)
    else:
        try:
            pdf_bytes = await render_invoice_pdf(pdf_context)
        except Exception as exc:
            logger.error("PDF render failed for plan_change approval request_id=%s err=%s", request_id, exc)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="pdf_render_failed") from exc

        try:
            await upload_pdf(settings.invoice_pdf_bucket, object_key, pdf_bytes, settings)
        except Exception as exc:
            logger.error("PDF upload failed request_id=%s err=%s", request_id, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="storage_upload_failed") from exc

    total_myr = proration.amount_due_myr
    proration_basis: dict[str, Any] = {
        "plan_change_request_id": str(request_id),
        "current_tier": current_tier.value,
        "target_tier": target_tier.value,
        "billing_cycle": billing_cycle_str,
        "ratio": str(proration.ratio),
        "days_billed": proration.days_billed,
        "days_in_period": proration.days_in_period,
        "quota_topup_bot_replies": proration.quota_topup_bot_replies,
    }
    due_at = datetime.now(UTC)

    async with conn.transaction():
        # tenant_invoices schema (migration 008 + 054):
        #   period TEXT, amount_myr NUMERIC — no invoice_kind / subtotal / tax columns yet.
        inv_row = await conn.fetchrow(
            "INSERT INTO tenant_invoices "
            "(id, tenant_id, invoice_number, period, amount_myr, proration_basis, "
            "pdf_bucket, pdf_object_key, status, due_at) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,'issued',$9) "
            "RETURNING id, invoice_number, amount_myr",
            invoice_id, req_row["tenant_id"], invoice_number, period_str,
            str(total_myr),
            # dict DIRECTLY — JSONB codec serialises once (services/audit.py).
            proration_basis,
            settings.invoice_pdf_bucket, object_key, due_at,
        )
        assert inv_row is not None

        decided_at = datetime.now(UTC)
        await conn.execute(
            "UPDATE plan_change_requests "
            "SET status = 'approved', decided_at = $1 "
            "WHERE id = $2",
            decided_at, request_id,
        )
        await conn.execute(
            "UPDATE tenants SET plan_tier = $1 WHERE id = $2",
            target_tier.value, req_row["tenant_id"],
        )
        await audit_record(
            conn,
            tenant_id=req_row["tenant_id"],
            actor=admin_id,
            action="plan_change.approved",
            before={"status": "pending", "plan_tier": current_tier.value},
            after={
                "status": "approved",
                "plan_tier": target_tier.value,
                "invoice_number": invoice_number,
                "amount_due_myr": str(total_myr),
                "plan_change_request_id": str(request_id),
            },
        )

    logger.info("plan_change.approved request_id=%s invoice=%s amount=%s actor=%s", request_id, invoice_number, total_myr, admin_id)
    return _ApproveResponse(
        request_id=request_id, status="approved",
        invoice_id=invoice_id, invoice_number=invoice_number,
        amount_due_myr=float(total_myr),
        approved_at=decided_at.isoformat(),
    )


@router.post(
    "/{request_id}/reject",
    response_model=_RejectResponse,
    summary="Reject a plan-change request",
)
async def reject_plan_change(
    request_id: UUID,
    body: _RejectBody,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _RejectResponse:
    req_row = await conn.fetchrow(
        "SELECT id, tenant_id, status FROM plan_change_requests WHERE id = $1",
        request_id,
    )
    if req_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan_change_not_found")
    if req_row["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot_reject_status_" + req_row["status"],
        )

    decided_at = datetime.now(UTC)
    async with conn.transaction():
        await conn.execute(
            "UPDATE plan_change_requests "
            "SET status = 'rejected', decided_at = $1, "
            "decision_note = $2 "
            "WHERE id = $3",
            decided_at, body.decision_note, request_id,
        )
        await audit_record(
            conn,
            tenant_id=req_row["tenant_id"],
            actor=admin_id,
            action="plan_change.rejected",
            before={"status": "pending"},
            after={
                "status": "rejected",
                "decision_note": body.decision_note,
                "plan_change_request_id": str(request_id),
            },
        )

    logger.info("plan_change.rejected request_id=%s actor=%s", request_id, admin_id)
    return _RejectResponse(
        request_id=request_id,
        status="rejected",
        decided_at=decided_at.isoformat(),
    )
