"""Admin billing routes — Phase 9 PR-5.

Mounted at ``/api/admin/billing`` (registered in main.py).

Endpoints:
  * ``POST /invoices``               — issue a new invoice
  * ``GET  /invoices``               — list with optional filters
  * ``GET  /invoices/{id}/pdf``      — 307 redirect to signed PDF URL
  * ``POST /invoices/{id}/mark-paid`` — mark an invoice as paid
  * ``GET  /archive``                — invoice counts grouped by (YYYY-MM, tenant_id)

Schema (migration 008 + 054 ALTER):
  tenant_invoices columns used here:
    id, tenant_id, invoice_number, period (TEXT e.g. '2026-05'),
    amount_myr, status, proration_basis, pdf_url, pdf_bucket,
    pdf_object_key, issued_at, paid_at, due_at, marked_paid_by_admin_id,
    void_reason, created_at, updated_at

Auth: ``require_platform_admin``.
ADR: ADR-028 (invoice format), ADR-030 (audit every write).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from polaris_api.config import get_settings
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record
from polaris_api.services.billing.invoice_sequencer import next_invoice_number
from polaris_api.services.billing.pdf_renderer import render_invoice_pdf
from polaris_api.services.billing.storage import get_signed_url, upload_pdf
from polaris_api.utils.errors import api_error

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-billing"])
_FORBID = ConfigDict(extra="forbid")

_VALID_STATUSES = frozenset({"issued", "paid", "overdue", "void"})
_INVOICE_KINDS = frozenset(
    {"monthly_subscription", "annual_subscription", "plan_change_proration", "addon_seat"}
)


class _IssueInvoiceRequest(BaseModel):
    model_config = _FORBID
    tenant_id: UUID
    invoice_kind: str = Field(
        ...,
        description="monthly_subscription | annual_subscription | plan_change_proration | addon_seat",
    )
    period_start: date
    period_end: date
    subtotal_myr: float = Field(..., gt=0)
    tax_myr: float = Field(default=0, ge=0)
    due_at: datetime
    proration_basis: dict[str, Any] = Field(default_factory=dict)

    @field_validator("invoice_kind")
    @classmethod
    def _kind_valid(cls, v: str) -> str:
        if v not in _INVOICE_KINDS:
            raise ValueError(f"invoice_kind must be one of {sorted(_INVOICE_KINDS)}")
        return v

    @property
    def total_myr(self) -> float:
        return round(self.subtotal_myr + self.tax_myr, 2)

    @property
    def period(self) -> str:
        return self.period_start.strftime("%Y-%m")

    @property
    def billing_cycle(self) -> str:
        return "annual" if self.invoice_kind == "annual_subscription" else "monthly"


class _TenantInvoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    tenant_id: UUID
    tenant_name: str | None = None
    invoice_number: str
    invoice_kind: str | None
    period: str
    period_start: date | None
    period_end: date | None
    subtotal_myr: float
    tax_myr: float
    total_myr: float
    amount_myr: float
    status: str
    issued_at: datetime
    due_at: datetime | None
    paid_at: datetime | None
    marked_paid_by_admin_id: UUID | None
    void_reason: str | None
    pdf_bucket: str | None
    pdf_object_key: str | None
    created_at: datetime
    updated_at: datetime


class _ListInvoicesResponse(BaseModel):
    model_config = _FORBID
    items: list[_TenantInvoice]
    total: int
    offset: int
    limit: int
    next_cursor: UUID | None = None


class _MarkPaidResponse(BaseModel):
    model_config = _FORBID
    id: UUID
    status: str
    paid_at: str
    marked_paid_by_admin_id: UUID


class _VoidRequest(BaseModel):
    model_config = _FORBID
    void_reason: str = Field(..., min_length=1, max_length=500)


class _VoidResponse(BaseModel):
    model_config = _FORBID
    id: UUID
    status: str
    void_reason: str


class _ArchiveRow(BaseModel):
    model_config = _FORBID
    year_month: str
    tenant_id: UUID
    count: int


class _ArchiveResponse(BaseModel):
    model_config = _FORBID
    items: list[_ArchiveRow]


_SELECT_COLS = (
    "id, tenant_id, invoice_number, invoice_kind, period, period_start, period_end, "
    "subtotal_myr, tax_myr, total_myr, amount_myr, status, "
    "pdf_bucket, pdf_object_key, issued_at, due_at, paid_at, "
    "marked_paid_by_admin_id, void_reason, created_at, updated_at"
)

# Qualified variant for the joined list query (mirrors admin_plan_changes.py's
# alias-qualified _SELECT_COLS style). Every field _TenantInvoice declares
# (besides tenant_name, added separately via the LEFT JOIN) must appear here.
_LIST_SELECT_COLS = (
    "i.id, i.tenant_id, i.invoice_number, i.invoice_kind, i.period, "
    "i.period_start, i.period_end, i.subtotal_myr, i.tax_myr, i.total_myr, "
    "i.amount_myr, i.status, i.pdf_bucket, i.pdf_object_key, i.issued_at, "
    "i.due_at, i.paid_at, i.marked_paid_by_admin_id, i.void_reason, "
    "i.created_at, i.updated_at"
)


def _num(value: Any) -> float:
    """Coerce a numeric/None column to float (NULL → 0.0 for legacy rows)."""
    return float(value) if value is not None else 0.0


def _row_to_invoice(row: asyncpg.Record) -> _TenantInvoice:
    return _TenantInvoice(
        id=row["id"],
        tenant_id=row["tenant_id"],
        # Present on the list query (LEFT JOIN tenants); absent on RETURNING-based
        # single-row responses (issue/mark-paid/void) which don't join. asyncpg
        # Record raises KeyError on a missing column, so gate on keys().
        tenant_name=(row["tenant_name"] if "tenant_name" in row.keys() else None),
        invoice_number=row["invoice_number"],
        invoice_kind=row["invoice_kind"],
        period=row["period"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        subtotal_myr=_num(row["subtotal_myr"]),
        tax_myr=_num(row["tax_myr"]),
        total_myr=_num(row["total_myr"]),
        amount_myr=_num(row["amount_myr"]),
        status=row["status"],
        issued_at=row["issued_at"],
        due_at=row["due_at"],
        paid_at=row["paid_at"],
        marked_paid_by_admin_id=row["marked_paid_by_admin_id"],
        void_reason=row["void_reason"],
        pdf_bucket=row["pdf_bucket"],
        pdf_object_key=row["pdf_object_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _build_invoice_pdf_context(
    invoice_number: str,
    body: _IssueInvoiceRequest,
    tenant_name: str,
) -> dict[str, Any]:
    return {
        "invoice_number": invoice_number,
        "invoice_kind": body.invoice_kind,
        "period": body.period,
        "period_start": body.period_start.strftime("%Y-%m-%d"),
        "period_end": body.period_end.strftime("%Y-%m-%d"),
        "subtotal_myr": f"{body.subtotal_myr:.2f}",
        "tax_myr": f"{body.tax_myr:.2f}",
        "total_myr": f"{body.total_myr:.2f}",
        # Back-compat key the existing invoice.html.j2 template renders (= total).
        "amount_myr": f"{body.total_myr:.2f}",
        "tenant_name": tenant_name,
        "issued_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "due_date": body.due_at.strftime("%Y-%m-%d"),
    }


@router.post(
    "/invoices",
    response_model=_TenantInvoice,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a new tenant invoice",
)
async def issue_invoice(
    body: _IssueInvoiceRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _TenantInvoice:
    """Issue invoice: validate → tenant check → POL number → PDF → upload → DB + audit."""
    tenant_row = await conn.fetchrow(
        "SELECT id, display_name FROM tenants WHERE id = $1 LIMIT 1",
        body.tenant_id,
    )
    if tenant_row is None:
        raise api_error("tenant_not_found", status.HTTP_404_NOT_FOUND)
    tenant_name: str = tenant_row["display_name"] or str(body.tenant_id)

    billing_cycle = body.billing_cycle
    settings = get_settings()
    invoice_number = await next_invoice_number(conn, billing_cycle)
    pdf_context = _build_invoice_pdf_context(invoice_number, body, tenant_name)

    try:
        pdf_bytes = await render_invoice_pdf(pdf_context)
    except Exception as exc:
        logger.error("PDF render failed invoice=%s err=%s", invoice_number, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="pdf_render_failed",
        ) from exc

    invoice_id = uuid4()
    object_key = f"invoices/{body.tenant_id}/{invoice_id}.pdf"
    try:
        await upload_pdf(settings.invoice_pdf_bucket, object_key, pdf_bytes, settings)
    except Exception as exc:
        logger.error("PDF upload failed invoice=%s err=%s", invoice_number, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="storage_upload_failed",
        ) from exc

    async with conn.transaction():
        row = await conn.fetchrow(
            f"""
            INSERT INTO tenant_invoices
              (id, tenant_id, invoice_number, invoice_kind, period,
               period_start, period_end, subtotal_myr, tax_myr, total_myr,
               amount_myr, proration_basis, pdf_bucket, pdf_object_key, status, due_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12::jsonb, $13, $14, 'issued', $15)
            RETURNING {_SELECT_COLS}
            """,  # noqa: S608 — _SELECT_COLS is a hardcoded constant, not user input
            invoice_id,
            body.tenant_id,
            invoice_number,
            body.invoice_kind,
            body.period,
            body.period_start,
            body.period_end,
            str(body.subtotal_myr),
            str(body.tax_myr),
            str(body.total_myr),
            str(body.total_myr),  # amount_myr kept = total for back-compat
            # dict DIRECTLY — JSONB codec serialises once (services/audit.py).
            body.proration_basis,
            settings.invoice_pdf_bucket,
            object_key,
            body.due_at,
        )
        assert row is not None  # noqa: S101
        await audit_record(
            conn,
            tenant_id=body.tenant_id,
            actor=admin_id,
            action="invoice.issued",
            after={
                "invoice_number": invoice_number,
                "invoice_kind": body.invoice_kind,
                "period": body.period,
                "total_myr": str(body.total_myr),
            },
        )

    logger.info(
        "invoice.issued number=%s tenant=%s actor=%s",
        invoice_number,
        body.tenant_id,
        admin_id,
    )
    return _row_to_invoice(row)


@router.get(
    "/invoices",
    response_model=_ListInvoicesResponse,
    summary="List invoices with optional filters",
)
async def list_invoices(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    status_filter: str | None = Query(default=None, alias="status"),
    tenant_id: Annotated[UUID | None, Query()] = None,
    year_month: str | None = Query(default=None, description="Filter by period in YYYY-MM format"),
    cursor: Annotated[UUID | None, Query(description="Last seen invoice id")] = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> _ListInvoicesResponse:
    """List invoices with optional filters, keyset-paginated on (issued_at, id).

    ``offset``/``limit`` remain accepted for backward compatibility with the
    existing zero-param admin BillingPage caller (``offset`` is echoed back
    but no longer used to skip rows — keyset via ``cursor`` replaces it).
    """
    if status_filter is not None and status_filter not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(_VALID_STATUSES)}",
        )
    conditions: list[str] = []
    params: list[Any] = []
    p = 1
    if status_filter:
        conditions.append(f"i.status = ${p}")
        params.append(status_filter)
        p += 1
    if tenant_id:
        conditions.append(f"i.tenant_id = ${p}")
        params.append(tenant_id)
        p += 1
    if year_month:
        # period column stores values like '2026-05'; match prefix for the month filter.
        try:
            datetime.strptime(year_month, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="year_month must be YYYY-MM",
            ) from exc
        conditions.append(f"i.period LIKE ${p}")
        params.append(f"{year_month}%")
        p += 1
    if cursor is not None:
        cursor_row = await conn.fetchrow(
            "SELECT issued_at, id FROM tenant_invoices WHERE id = $1", cursor
        )
        if cursor_row is None:
            raise HTTPException(status_code=422, detail="invalid_cursor")
        conditions.append(f"(i.issued_at, i.id) < (${p}, ${p + 1})")
        params.append(cursor_row["issued_at"])
        params.append(cursor_row["id"])
        p += 2

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    total = (
        await conn.fetchval(
            f"SELECT COUNT(*) FROM tenant_invoices i {where_clause}",  # noqa: S608
            *params,
        )
        or 0
    )
    rows = await conn.fetch(
        f"SELECT {_LIST_SELECT_COLS}, t.display_name AS tenant_name "  # noqa: S608
        f"FROM tenant_invoices i LEFT JOIN tenants t ON t.id = i.tenant_id "
        f"{where_clause} ORDER BY i.issued_at DESC, i.id DESC LIMIT ${p}",
        *params,
        limit + 1,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    nxt: UUID | None = page[-1]["id"] if has_more and page else None
    return _ListInvoicesResponse(
        items=[_row_to_invoice(r) for r in page],
        total=int(total),
        offset=offset,
        limit=limit,
        next_cursor=nxt,
    )


@router.get(
    "/invoices/{invoice_id}/pdf",
    summary="Redirect to signed PDF URL",
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
)
async def get_invoice_pdf(
    invoice_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> Response:
    """Fetch a signed Supabase Storage URL and 307-redirect to it."""
    row = await conn.fetchrow(
        "SELECT pdf_bucket, pdf_object_key FROM tenant_invoices WHERE id = $1",
        invoice_id,
    )
    if row is None:
        raise api_error("invoice_not_found", status.HTTP_404_NOT_FOUND)
    if not row["pdf_object_key"]:
        raise api_error("pdf_not_available", status.HTTP_404_NOT_FOUND)
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
    "/invoices/{invoice_id}/mark-paid",
    response_model=_MarkPaidResponse,
    summary="Mark invoice as paid",
)
async def mark_invoice_paid(
    invoice_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _MarkPaidResponse:
    """Flip invoice status to paid; record paid_at and actor. Idempotent if already paid."""
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, tenant_id, invoice_number, status, paid_at, marked_paid_by_admin_id "
            "FROM tenant_invoices WHERE id = $1",
            invoice_id,
        )
        if row is None:
            raise api_error("invoice_not_found", status.HTTP_404_NOT_FOUND)
        if row["status"] == "paid":
            return _MarkPaidResponse(
                id=invoice_id,
                status="paid",
                paid_at=row["paid_at"].isoformat(),
                marked_paid_by_admin_id=row["marked_paid_by_admin_id"],
            )
        if row["status"] == "void":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot_mark_void_invoice_as_paid",
            )
        paid_at = datetime.now(UTC)
        await conn.execute(
            "UPDATE tenant_invoices "
            "SET status='paid', paid_at=$1, marked_paid_by_admin_id=$2, updated_at=NOW() "
            "WHERE id=$3",
            paid_at,
            admin_id,
            invoice_id,
        )
        await audit_record(
            conn,
            tenant_id=row["tenant_id"],
            actor=admin_id,
            action="invoice.marked_paid",
            before={"status": row["status"]},
            after={
                "status": "paid",
                "invoice_number": row["invoice_number"],
                "paid_at": paid_at.isoformat(),
            },
        )
    logger.info(
        "invoice.marked_paid id=%s number=%s actor=%s",
        invoice_id,
        row["invoice_number"],
        admin_id,
    )
    return _MarkPaidResponse(
        id=invoice_id,
        status="paid",
        paid_at=paid_at.isoformat(),
        marked_paid_by_admin_id=admin_id,
    )


@router.post(
    "/invoices/{invoice_id}/void",
    response_model=_VoidResponse,
    summary="Void an invoice",
)
async def void_invoice(
    invoice_id: UUID,
    body: _VoidRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _VoidResponse:
    """Void an issued/overdue invoice with a required reason. Idempotent
    if already void (returns the existing reason). A paid invoice cannot
    be voided — issue a credit instead (409).
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, tenant_id, invoice_number, status, void_reason "
            "FROM tenant_invoices WHERE id = $1",
            invoice_id,
        )
        if row is None:
            raise api_error("invoice_not_found", status.HTTP_404_NOT_FOUND)
        if row["status"] == "void":
            return _VoidResponse(
                id=invoice_id,
                status="void",
                void_reason=row["void_reason"] or body.void_reason,
            )
        if row["status"] == "paid":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot_void_paid_invoice",
            )
        await conn.execute(
            "UPDATE tenant_invoices "
            "SET status='void', void_reason=$1, updated_at=NOW() "
            "WHERE id=$2",
            body.void_reason,
            invoice_id,
        )
        await audit_record(
            conn,
            tenant_id=row["tenant_id"],
            actor=admin_id,
            action="invoice.voided",
            before={"status": row["status"]},
            after={
                "status": "void",
                "invoice_number": row["invoice_number"],
                "void_reason": body.void_reason,
            },
        )
    logger.info(
        "invoice.voided id=%s number=%s actor=%s",
        invoice_id,
        row["invoice_number"],
        admin_id,
    )
    return _VoidResponse(
        id=invoice_id,
        status="void",
        void_reason=body.void_reason,
    )


@router.get(
    "/archive",
    response_model=_ArchiveResponse,
    summary="Invoice archive aggregates by (year_month, tenant_id)",
)
async def get_archive(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _ArchiveResponse:
    """Return invoice counts grouped by (YYYY-MM period, tenant_id)."""
    rows = await conn.fetch(
        """
        SELECT
          CASE
            WHEN period ~ '^[0-9]{4}-[0-9]{2}' THEN LEFT(period, 7)
            ELSE period
          END AS year_month,
          tenant_id,
          COUNT(*)::int AS count
        FROM tenant_invoices
        WHERE period IS NOT NULL
        GROUP BY year_month, tenant_id
        ORDER BY year_month DESC, tenant_id
        """
    )
    return _ArchiveResponse(
        items=[
            _ArchiveRow(
                year_month=r["year_month"],
                tenant_id=r["tenant_id"],
                count=r["count"],
            )
            for r in rows
        ]
    )
