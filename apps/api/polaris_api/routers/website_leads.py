"""Admin website-leads viewer + lifecycle endpoints.

Mounted at /api/admin/leads.

Backs the marketing-site lead capture that replaced Google Apps Script: a
Cloudflare Pages Function writes rows into ``website_leads`` (migration 115) via
the Supabase REST API; these endpoints let a platform admin browse, mark
contacted, and export them.

Three endpoints:
  * ``GET    /``                — filterable (status, form_type) cursor-paginated list
  * ``PATCH  /{lead_id}/contacted`` — flip a 'new' lead → 'contacted' (one-way)
  * ``GET    /export``          — CSV of the active filter (no pagination)

Auth: ``require_platform_admin`` (shared with the other admin routers). Reads +
writes go through the BYPASSRLS ``polaris_service`` pool via
``get_admin_service_conn``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin.pool import get_admin_service_conn

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-leads"])
_FORBID = ConfigDict(extra="forbid")

# Hard cap on a single CSV export — log if a filter would exceed it.
_EXPORT_LIMIT = 5000

# Common per-form-type keys flattened into their own CSV columns.
_DETAIL_COLUMNS = (
    "plan",
    "template",
    "has_domain",
    "inquiry",
    "website_url",
    "heard_from",
    "website_consent",
    "contact_consent",
    "consent",
)

_SELECT_COLUMNS = (
    "id, form_type, name, email, phone, business_name, industry, message,"
    " details, status, contacted_at, created_at"
)


class _WebsiteLeadItem(BaseModel):
    model_config = _FORBID
    id: UUID
    form_type: str
    name: str
    email: str
    phone: str | None
    business_name: str | None
    industry: str | None
    message: str | None
    details: Any
    status: str
    contacted_at: str | None
    created_at: str


class _WebsiteLeadsResponse(BaseModel):
    model_config = _FORBID
    items: list[_WebsiteLeadItem]
    next_cursor: UUID | None


def _details(row: asyncpg.Record) -> Any:
    # asyncpg's JSONB codec usually yields a dict, but guard for the str case.
    raw = row["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


def _row_to_item(row: asyncpg.Record) -> _WebsiteLeadItem:
    contacted = row["contacted_at"]
    return _WebsiteLeadItem(
        id=row["id"],
        form_type=row["form_type"],
        name=row["name"],
        email=row["email"],
        phone=row["phone"],
        business_name=row["business_name"],
        industry=row["industry"],
        message=row["message"],
        details=_details(row),
        status=row["status"],
        contacted_at=(contacted.astimezone(UTC).isoformat() if contacted is not None else None),
        created_at=row["created_at"].astimezone(UTC).isoformat(),
    )


@router.get("/", response_model=_WebsiteLeadsResponse)
async def list_leads(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    status: Annotated[Literal["new", "contacted"], Query()] = "new",
    form_type: Annotated[
        Literal["demo", "pricing", "contact", "website"] | None, Query()
    ] = None,
    cursor: Annotated[UUID | None, Query(description="Last seen id")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> _WebsiteLeadsResponse:
    """Cursor-paginated leads, newest first, filtered by status (+ optional type)."""
    conditions: list[str] = ["wl.status = $1"]
    args: list[Any] = [status]
    idx = 2
    if form_type is not None:
        conditions.append(f"wl.form_type = ${idx}")
        args.append(form_type)
        idx += 1
    if cursor is not None:
        cursor_row = await conn.fetchrow(
            "SELECT created_at, id FROM website_leads WHERE id = $1", cursor
        )
        if cursor_row is None:
            raise HTTPException(status_code=400, detail="invalid_cursor")
        conditions.append(f"(wl.created_at, wl.id) < (${idx}, ${idx + 1})")
        args.append(cursor_row["created_at"])
        args.append(cursor_row["id"])
        idx += 2
    where_clause = "WHERE " + " AND ".join(conditions)
    args.append(limit + 1)
    query = (
        f"SELECT {_SELECT_COLUMNS} FROM website_leads wl "  # noqa: S608
        + where_clause
        + f" ORDER BY wl.created_at DESC, wl.id DESC LIMIT ${idx}"
    )
    rows = await conn.fetch(query, *args)  # noqa: S608
    has_more = len(rows) > limit
    page = rows[:limit]
    nxt: UUID | None = page[-1]["id"] if has_more and page else None
    return _WebsiteLeadsResponse(items=[_row_to_item(r) for r in page], next_cursor=nxt)


@router.patch("/{lead_id}/contacted", response_model=_WebsiteLeadItem)
async def mark_contacted(
    lead_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _WebsiteLeadItem:
    """Mark a lead contacted (one-way). Records who actioned it and when."""
    update_sql = (
        "UPDATE website_leads"  # noqa: S608
        " SET status = 'contacted', contacted_at = now(), contacted_by = $2"
        f" WHERE id = $1 RETURNING {_SELECT_COLUMNS}"
    )
    row = await conn.fetchrow(update_sql, lead_id, admin_id)
    if row is None:
        raise HTTPException(status_code=404, detail="lead_not_found")
    return _row_to_item(row)


@router.get("/export")
async def export_leads(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    status: Annotated[Literal["new", "contacted"], Query()] = "new",
    form_type: Annotated[
        Literal["demo", "pricing", "contact", "website"] | None, Query()
    ] = None,
) -> Response:
    """CSV of the active filter (no pagination, capped at _EXPORT_LIMIT rows)."""
    conditions: list[str] = ["status = $1"]
    args: list[Any] = [status]
    idx = 2
    if form_type is not None:
        conditions.append(f"form_type = ${idx}")
        args.append(form_type)
        idx += 1
    args.append(_EXPORT_LIMIT + 1)
    query = (
        f"SELECT {_SELECT_COLUMNS} FROM website_leads "  # noqa: S608
        + "WHERE "
        + " AND ".join(conditions)
        + f" ORDER BY created_at DESC, id DESC LIMIT ${idx}"
    )
    rows = await conn.fetch(query, *args)  # noqa: S608
    if len(rows) > _EXPORT_LIMIT:
        logger.warning(
            "website_leads export truncated at %d rows (status=%s form_type=%s)",
            _EXPORT_LIMIT,
            status,
            form_type,
        )
        rows = rows[:_EXPORT_LIMIT]

    buf = io.StringIO()
    writer = csv.writer(buf)
    header = [
        "created_at",
        "form_type",
        "name",
        "email",
        "phone",
        "business_name",
        "industry",
        "message",
        "status",
        "contacted_at",
        *_DETAIL_COLUMNS,
    ]
    writer.writerow(header)
    for row in rows:
        details = _details(row) or {}
        contacted = row["contacted_at"]
        writer.writerow(
            [
                row["created_at"].astimezone(UTC).isoformat(),
                row["form_type"],
                row["name"],
                row["email"],
                row["phone"] or "",
                row["business_name"] or "",
                row["industry"] or "",
                row["message"] or "",
                row["status"],
                contacted.astimezone(UTC).isoformat() if contacted is not None else "",
                *[_csv_cell(details.get(k)) for k in _DETAIL_COLUMNS],
            ]
        )

    suffix = f"-{form_type}" if form_type else ""
    filename = f"leads-{status}{suffix}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_cell(value: Any) -> str:
    """Render a details value for a CSV cell (bools → yes/no, None → '')."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
