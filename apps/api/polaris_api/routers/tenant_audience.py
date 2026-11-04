"""Tenant-facing audience API — Phase 6 PR-4.

Mounted at ``/api/tenant/audience`` per the standard prefix layering.

Four endpoints:

  * ``POST /import``                 — multipart CSV upload; runs the
                                       line-error-accumulating importer.
  * ``POST /import-from-conversations`` — scan messages for opt-in
                                       candidates (user-sent inbounds
                                       in the last N days, not opted
                                       out) and bulk-insert as
                                       audience_members with
                                       opt_in_source='api_sync'.
  * ``GET /opted-out``               — paginated JSON viewer
                                       (GA-blocking PDPA viewer).
  * ``GET /opted-out.csv``           — StreamingResponse CSV export.

The same opt-out viewer surface is mirrored under
``/api/v1/audience/opted-out`` (GA exception per ADR-035 Amendment
evening) — see ``routers/public_audience_optout.py``. We keep the
implementation in services/audience/opt_out so both routers share
one source of truth.

Caps from env (validated at router boundary, NOT inside the
importer):
  * ``AUDIENCE_CSV_MAX_BYTES`` (default 10 MB)
  * ``AUDIENCE_CSV_MAX_ROWS``  (default 100 000)
  * ``OPT_OUT_VIEWER_PAGE_SIZE`` (default 200)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    Query,
    UploadFile,
    status,
)
from fastapi import File as FileParam
from fastapi.responses import StreamingResponse
from polaris_shared_types import CSVImportResult, OptOutViewerPage
from pydantic import BaseModel, Field

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.audience import importer, opt_out
from polaris_api.utils.errors import api_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-audience"])

_DEFAULT_CSV_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_DEFAULT_CSV_MAX_ROWS = 100_000
_DEFAULT_OPT_OUT_PAGE_SIZE = 200


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("env var %s=%r not int; using default %d", name, raw, default)
        return default


async def _tenant_default_country_code(
    conn: asyncpg.Connection, tenant_id: str
) -> str:
    """Read tenants.default_country_code under RLS. Defaults to 'MY'
    per ADR-035 Amendment (Phase 1 / migration 001 sets the column
    default to 'MY')."""
    row = await conn.fetchrow(
        "SELECT default_country_code FROM tenants WHERE id = $1",
        tenant_id,
    )
    if row and row["default_country_code"]:
        cc: str = row["default_country_code"]
        return cc
    return "MY"


# =====================================================================
# Phase 8 PR-8 — Members tab listing
# =====================================================================


class AudienceMemberListItem(BaseModel):
    """One row in ``GET /api/tenant/audience/``. Drives the Members
    tab of the portal Audience screen (#6).

    Excludes opted-out members — those live under ``/opted-out`` so the
    PDPA viewer is the only entry point for opt-out data per the GA
    compliance contract (portal_design_prompt.md §6).
    """

    id: str
    channel: str
    channel_user_id: str
    channel_user_name: str | None
    phone_number_e164: str | None
    email: str | None
    locale: str | None
    tags: list[str] = Field(default_factory=list)
    opt_in_source: str
    opted_in_at: str
    tenant_branch_id: str | None


class AudienceMemberListResponse(BaseModel):
    """Cursor-paginated payload. Mirrors notifications + conversations
    cursor shape so the portal table primitive is reused without
    divergence."""

    items: list[AudienceMemberListItem]
    next_cursor: str | None = None


_DEFAULT_MEMBERS_PAGE_SIZE = 50
_MEMBERS_PAGE_SIZE_MAX = 200


def _encode_member_cursor(opted_in_at_iso: str, member_id: str) -> str:
    return f"{opted_in_at_iso}|{member_id}"


def _decode_member_cursor(token: str) -> tuple[datetime, str]:
    ts_part, _, id_part = token.partition("|")
    if not ts_part or not id_part:
        raise ValueError("cursor must be '<rfc3339>|<uuid>'")
    return datetime.fromisoformat(ts_part.replace("Z", "+00:00")), id_part


@router.get(
    "/",
    response_model=AudienceMemberListResponse,
    summary="List opted-in audience members (Members tab)",
)
async def list_members(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    page_size: Annotated[
        int, Query(ge=1, le=_MEMBERS_PAGE_SIZE_MAX)
    ] = _DEFAULT_MEMBERS_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    channel: Annotated[str | None, Query(max_length=32)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    intent: Annotated[str | None, Query(max_length=100)] = None,
) -> AudienceMemberListResponse:
    """Newest opt-ins first. Channel + free-text search filters.

    Search runs against ``channel_user_id``, ``phone_number_e164``,
    ``email``, and ``channel_user_name`` with case-insensitive LIKE.
    Bounded by page size, so the planner picks a table scan for low
    cardinality + the index for high — either way the response stays
    inside the portal's 500 ms p95 budget per portal_design_prompt §6.

    RLS scopes everything to the calling tenant; the route does not
    add a redundant tenant_id filter so a policy regression surfaces
    in tests, not in production.
    """
    where_parts = ["opted_out_at IS NULL"]
    args: list[object] = []

    if channel is not None:
        args.append(channel)
        where_parts.append(f"channel = ${len(args)}")

    if q is not None and q.strip():
        args.append(f"%{q.strip()}%")
        idx = len(args)
        where_parts.append(
            f"(channel_user_id ILIKE ${idx} "
            f"OR phone_number_e164 ILIKE ${idx} "
            f"OR email ILIKE ${idx} "
            f"OR channel_user_name ILIKE ${idx})"
        )

    if intent is not None:
        args.append(intent)
        where_parts.append(f"metadata->>'intent' = ${len(args)}")

    if cursor is not None:
        try:
            cursor_dt, cursor_id = _decode_member_cursor(cursor)
        except ValueError as exc:
            raise api_error(
                "AUDIENCE_INVALID_CURSOR",
                status.HTTP_400_BAD_REQUEST,
                message="cursor must be an opaque token from a prior next_cursor",
            ) from exc
        args.append(cursor_dt)
        args.append(cursor_id)
        where_parts.append(
            f"(opted_in_at, id) < (${len(args) - 1}, ${len(args)})"
        )

    where_sql = "WHERE " + " AND ".join(where_parts)
    args.append(page_size + 1)
    # Local-only literal SQL — `where_sql` is composed from a closed
    # set of column-equals-placeholder snippets; values bind via $N.
    sql = (
        "SELECT id, tenant_branch_id, channel, channel_user_id, "
        "channel_user_name, phone_number_e164, email, locale, tags, "
        "opt_in_source, opted_in_at\n"
        "FROM audience_members\n"
        f"{where_sql}\n"
        "ORDER BY opted_in_at DESC, id DESC\n"
        f"LIMIT ${len(args)}"
    )  # noqa: S608 — where_sql is locally-built literal snippets
    rows = await conn.fetch(sql, *args)

    has_more = len(rows) > page_size
    if has_more:
        rows = rows[:page_size]

    items = [
        AudienceMemberListItem(
            id=str(r["id"]),
            tenant_branch_id=(
                str(r["tenant_branch_id"]) if r["tenant_branch_id"] else None
            ),
            channel=r["channel"],
            channel_user_id=r["channel_user_id"],
            channel_user_name=r["channel_user_name"],
            phone_number_e164=r["phone_number_e164"],
            email=r["email"],
            locale=r["locale"],
            tags=list(r["tags"] or []),
            opt_in_source=r["opt_in_source"],
            opted_in_at=r["opted_in_at"].isoformat(),
        )
        for r in rows
    ]

    next_cursor: str | None = None
    if has_more and items:
        next_cursor = _encode_member_cursor(items[-1].opted_in_at, items[-1].id)

    return AudienceMemberListResponse(items=items, next_cursor=next_cursor)


@router.post(
    "/import",
    response_model=CSVImportResult,
    status_code=status.HTTP_200_OK,
    summary="Upload a CSV of audience members",
)
async def import_audience_csv(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    file: Annotated[UploadFile, FileParam(...)],
) -> CSVImportResult:
    """Stream the upload into memory + invoke the importer.

    Caps:
      * 413 if size > AUDIENCE_CSV_MAX_BYTES
      * 422 if row count > AUDIENCE_CSV_MAX_ROWS (informally — we let
        the importer succeed up to the cap; row-count enforcement
        happens by counting lines BEFORE calling importer.import_csv,
        since the importer doesn't itself enforce the cap)
    """
    max_bytes = _env_int("AUDIENCE_CSV_MAX_BYTES", _DEFAULT_CSV_MAX_BYTES)
    max_rows = _env_int("AUDIENCE_CSV_MAX_ROWS", _DEFAULT_CSV_MAX_ROWS)

    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise api_error(
            "AUDIENCE_CSV_TOO_LARGE",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            max_bytes=max_bytes,
            got_bytes=len(raw),
        )

    # Row-count guard. Cheap: count newlines in bytes (excl. final);
    # over-counts by 1 for files ending without newline + over-counts
    # by 1 for the header row, so the comparison vs max_rows+2 is a
    # forgiving boundary.
    approx_lines = raw.count(b"\n")
    if approx_lines > max_rows + 2:
        raise api_error(
            "AUDIENCE_CSV_TOO_MANY_ROWS",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            max_rows=max_rows,
            approx_rows=approx_lines - 1,  # minus header
        )

    country = await _tenant_default_country_code(conn, str(ctx.tenant_id))
    return await importer.import_csv(
        conn,
        tenant_id=ctx.tenant_id,
        raw_bytes=raw,
        default_country_code=country,
        opt_in_source="csv_upload",
    )


@router.post(
    "/import-from-conversations",
    response_model=CSVImportResult,
    status_code=status.HTTP_200_OK,
    summary="Bulk-insert opt-in candidates from recent inbounds",
)
async def import_from_conversations(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    since_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> CSVImportResult:
    """Scan messages.role='user' for distinct (channel, channel_user_id)
    in the last ``since_days`` and add them as audience_members with
    ``opt_in_source='api_sync'``.

    Skips contacts who are already opted-in (active row exists) or
    opted-out (we don't re-opt-in automatically). The result reports
    ``imported_count`` of newly-added active rows; ``skipped_count``
    of contacts that already had any row; ``error_rows`` stays empty
    because there's no per-row validation pass on this surface.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT c.channel, c.channel_user_id
          FROM conversations c
          JOIN messages m ON m.conversation_id = c.id
         WHERE m.role = 'user'
           AND m.created_at >= now() - ($1::int || ' days')::interval
        """,
        since_days,
    )

    imported = 0
    skipped = 0
    for row in rows:
        # Check whether an active or opted-out row already exists for
        # this (channel, channel_user_id). If either, skip — we never
        # silently re-opt-in someone who explicitly opted out.
        existing = await conn.fetchrow(
            """
            SELECT 1
              FROM audience_members
             WHERE channel = $1
               AND channel_user_id = $2
             LIMIT 1
            """,
            row["channel"],
            row["channel_user_id"],
        )
        if existing is not None:
            skipped += 1
            continue
        await conn.execute(
            """
            INSERT INTO audience_members
                (tenant_id, channel, channel_user_id, opt_in_source)
            VALUES ($1, $2, $3, 'api_sync')
            """,
            ctx.tenant_id,
            row["channel"],
            row["channel_user_id"],
        )
        imported += 1

    logger.info(
        "audience.import_from_conversations: tenant=%s since_days=%d "
        "imported=%d skipped=%d",
        ctx.tenant_id,
        since_days,
        imported,
        skipped,
    )

    return CSVImportResult(
        imported_count=imported,
        skipped_count=skipped,
        error_rows=[],
    )


@router.get(
    "/opted-out",
    response_model=OptOutViewerPage,
    summary="GA-blocking PDPA opt-out viewer (paginated)",
)
async def list_opted_out(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    channel: str | None = None,
    opted_out_since: datetime | None = None,
    cursor: str | None = None,
    page_size: int = Query(default=0, ge=0, le=1000),
) -> OptOutViewerPage:
    """Cursor-based pagination over opted-out members.

    page_size=0 → use OPT_OUT_VIEWER_PAGE_SIZE default. We use the
    request-conn TenantContext so RLS strips other tenants
    automatically — the WHERE clause inside list_opted_out doesn't
    need a tenant_id filter.
    """
    effective_page_size = page_size or _env_int(
        "OPT_OUT_VIEWER_PAGE_SIZE", _DEFAULT_OPT_OUT_PAGE_SIZE
    )
    return await opt_out.list_opted_out(
        conn,
        channel=channel,
        opted_out_since=opted_out_since,
        cursor=cursor,
        page_size=effective_page_size,
    )


@router.get(
    "/opted-out.csv",
    response_class=StreamingResponse,
    summary="GA-blocking PDPA opt-out viewer (CSV export)",
)
async def stream_opted_out_csv(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    channel: str | None = None,
    opted_out_since: datetime | None = None,
) -> StreamingResponse:
    """Streaming CSV — memory bounded for 100k+ row tenants.

    The async generator inside opt_out.stream_opted_out_csv pulls
    chunks of 1000 rows under the request-scoped connection. The
    transaction stays open for the duration of the response, which
    is acceptable for an export endpoint (closed when the client
    disconnects or the stream completes).
    """
    return StreamingResponse(
        opt_out.stream_opted_out_csv(
            conn,
            channel=channel,
            opted_out_since=opted_out_since,
        ),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                'attachment; filename="opted-out.csv"'
            ),
        },
    )
