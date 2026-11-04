"""Tenant-facing notification feed API — Phase 8 PR-1.

Mounted at ``/api/tenant/notifications`` per the standard prefix layering.

Three endpoints per 04i §5.5 + §6.1:

  * ``GET    /``                  — paginated list. Drives the bell-icon
                                    30 s poll (Q-PHASE8-08, tab-visibility
                                    -gated client-side) and the full-page
                                    filtered view. Cursor pagination via
                                    opaque ``cursor`` query param; optional
                                    ``since`` filter for incremental polls.
  * ``PATCH  /{notification_id}`` — mark a single notification read/unread.
                                    Idempotent — repeated calls with the
                                    same body are no-ops.
  * ``POST   /mark-all-read``     — bulk mark all unread notifications read
                                    for the calling tenant. Returns the
                                    count of rows transitioned.

Auth: standard ``get_tenant_context`` portal-JWT flow. RLS on
``notifications`` (migration 051) scopes everything to the calling
tenant_id; the router code does NOT add a redundant ``WHERE tenant_id =
…`` filter — that would mask RLS misconfiguration during local dev.

Backing reads exploit the partial indexes from migration 051:
  - ``notifications_tenant_unread_idx`` for the unread_count + the
    "show me what I missed" first-page fetch.
  - The non-partial scan only fires when the caller paginates back
    through read history (rare; bounded by cursor + page size).

Page size capped via ``NOTIFICATIONS_PAGE_SIZE_MAX`` env (default 50).
Bell-icon dropdown typically requests page_size=10; full-page view 50.

Error envelope codes:

  * ``NOTIFICATION_NOT_FOUND``  — id missing or wrong tenant (RLS strips).
  * ``NOTIFICATIONS_INVALID_CURSOR`` — malformed opaque cursor.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from polaris_shared_types.notifications import (
    Notification,
    NotificationKind,
    NotificationListResponse,
    NotificationMarkAllReadResponse,
    NotificationMarkReadRequest,
)

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-notifications"])

_DEFAULT_PAGE_SIZE = 20
_DEFAULT_PAGE_SIZE_MAX = 50


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("env var %s=%r not int; using default %d", name, raw, default)
        return default


# Evaluated once at module load — env vars don't change during a process
# lifetime, so re-reading on every request is wasted work. Pass-through
# to the Query() le= bound below so the OpenAPI contract matches the
# enforced cap.
_PAGE_SIZE_MAX: int = _env_int("NOTIFICATIONS_PAGE_SIZE_MAX", _DEFAULT_PAGE_SIZE_MAX)


def _encode_cursor(created_at: str, notification_id: UUID) -> str:
    """Composite cursor: ``<rfc3339>|<uuid>``.

    Pairs with the ORDER BY ``(created_at DESC, id DESC)`` so paging
    survives equal-millisecond bursts (broadcast completion can fire
    many notifications at the same ``now()`` value).
    """
    return f"{created_at}|{notification_id}"


def _decode_cursor(token: str) -> tuple[datetime, UUID]:
    """Parse a cursor token into (created_at, id).

    Raises ValueError if malformed; the caller maps that to HTTP 400.
    """
    ts_part, _, id_part = token.partition("|")
    if not ts_part or not id_part:
        raise ValueError("cursor must be '<rfc3339>|<uuid>'")
    ts = datetime.fromisoformat(ts_part.replace("Z", "+00:00"))
    nid = UUID(id_part)
    return ts, nid


def _row_to_notification(row: asyncpg.Record) -> Notification:
    """Map a `notifications` row to the Pydantic shape.

    asyncpg returns datetime objects for TIMESTAMPTZ; we isoformat them
    here because the wire contract is RFC3339 UTC strings (consistent
    with the rest of the shared-types modules — see handoff_event.py
    docstring).
    """
    return Notification(
        id=row["id"],
        tenant_id=row["tenant_id"],
        recipient_user_id=row["recipient_user_id"],
        kind=NotificationKind(row["kind"]),
        title=row["title"],
        body=row["body"],
        href=row["href"],
        metadata=row["metadata"] or {},
        read_at=row["read_at"].isoformat() if row["read_at"] else None,
        created_at=row["created_at"].isoformat(),
    )


# =====================================================================
# List (GET /)
# =====================================================================

# Two-arg ordering ((created_at, id) DESC) is the cursor tie-breaker:
# when two notifications share the same created_at (rare but possible
# under millisecond-rounded clocks), id provides a deterministic order
# so pagination doesn't skip or duplicate rows.
#
# We hydrate metadata via JSONB→Python conversion in asyncpg; the column
# default is '{}'::jsonb so it's never NULL, but we coalesce defensively.
_LIST_BASE_SQL = """
SELECT id,
       tenant_id,
       recipient_user_id,
       kind,
       title,
       body,
       href,
       metadata,
       read_at,
       created_at
  FROM notifications
"""

_UNREAD_COUNT_SQL = """
SELECT COUNT(*) AS n
  FROM notifications
 WHERE read_at IS NULL
"""


@router.get(
    "/",
    response_model=NotificationListResponse,
    summary="List notifications for the calling tenant",
)
async def list_notifications(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    page_size: Annotated[int, Query(ge=1, le=_PAGE_SIZE_MAX)] = _DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    since: Annotated[str | None, Query(max_length=64)] = None,
    unread_only: Annotated[bool, Query()] = False,
) -> NotificationListResponse:
    """List notifications, newest first.

    ``cursor``      — opaque pagination token from the previous response's
                      ``next_cursor``. NULL/absent on the first page.
                      Composite ``<rfc3339>|<uuid>`` so equal-millisecond
                      bursts (broadcast completion writes many rows at the
                      same ``now()``) don't cause page-boundary duplicates
                      or skips. Format is opaque from the contract's
                      perspective — clients pass it back verbatim.
    ``since``       — RFC3339 UTC. If supplied, only returns notifications
                      with ``created_at > since``. Drives the incremental
                      poll: client sends ``?since=<last_seen_at>`` and gets
                      back only what arrived since.
    ``unread_only`` — when true, restricts to ``read_at IS NULL``. Used by
                      the bell-icon dropdown's "show me what I missed"
                      first paint; full-page view passes false.
    """
    # Build the WHERE clause incrementally. RLS handles tenant scope —
    # we only add the optional filters here.
    where_parts: list[str] = []
    args: list[object] = []

    if unread_only:
        where_parts.append("read_at IS NULL")

    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "NOTIFICATIONS_INVALID_SINCE",
                    "message": "since must be RFC3339 UTC (e.g. 2026-05-20T00:00:00+00:00)",
                },
            ) from exc
        args.append(since_dt)
        where_parts.append(f"created_at > ${len(args)}")

    if cursor is not None:
        try:
            cursor_dt, cursor_id = _decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "NOTIFICATIONS_INVALID_CURSOR",
                    "message": "cursor must be an opaque token from a prior next_cursor",
                },
            ) from exc
        # Row-value comparison matches the ORDER BY (created_at, id) DESC
        # so a tie on created_at is broken by id without skipping the
        # boundary row.
        args.append(cursor_dt)
        args.append(cursor_id)
        where_parts.append(
            f"(created_at, id) < (${len(args) - 1}, ${len(args)})"
        )

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    # Fetch page_size+1 so we know whether to emit a next_cursor without
    # a separate COUNT query. The extra row is discarded before
    # returning if present.
    args.append(page_size + 1)
    list_sql = (
        f"{_LIST_BASE_SQL}"
        f"{where_sql}\n"
        f"ORDER BY created_at DESC, id DESC\n"
        f"LIMIT ${len(args)}"
    )
    rows = await conn.fetch(list_sql, *args)

    has_more = len(rows) > page_size
    if has_more:
        rows = rows[:page_size]

    items = [_row_to_notification(r) for r in rows]
    next_cursor = _encode_cursor(items[-1].created_at, items[-1].id) if has_more else None

    # unread_count is global for this tenant (not filtered by since /
    # cursor) — it powers the bell-icon badge, which doesn't care about
    # pagination state. RLS scopes the COUNT to the calling tenant.
    unread_row = await conn.fetchrow(_UNREAD_COUNT_SQL)
    unread_count = int(unread_row["n"]) if unread_row else 0

    return NotificationListResponse(
        items=items,
        unread_count=unread_count,
        next_cursor=next_cursor,
    )


# =====================================================================
# Mark single read/unread (PATCH /{id})
# =====================================================================


_MARK_READ_SQL = """
UPDATE notifications
   SET read_at = CASE WHEN $2::boolean THEN COALESCE(read_at, now()) ELSE NULL END
 WHERE id = $1
RETURNING id,
          tenant_id,
          recipient_user_id,
          kind,
          title,
          body,
          href,
          metadata,
          read_at,
          created_at
"""


@router.patch(
    "/{notification_id}",
    response_model=Notification,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "Notification not visible to the calling tenant"},
    },
    summary="Mark a notification read or unread",
)
async def mark_one(
    notification_id: UUID,
    body: NotificationMarkReadRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> Notification:
    """Mark a notification read/unread.

    Idempotent: setting read=true on an already-read notification keeps
    the original read_at timestamp (COALESCE in SQL). Setting read=false
    on an already-unread notification is a no-op write that returns the
    current row.

    404 if the ID is not visible to the calling tenant — covers both
    "doesn't exist" and "exists in another tenant" (RLS strips it before
    the UPDATE sees it). We don't distinguish the two so we don't leak
    cross-tenant existence.
    """
    row = await conn.fetchrow(_MARK_READ_SQL, notification_id, body.read)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NOTIFICATION_NOT_FOUND",
                "message": f"notification {notification_id} not found",
            },
        )
    return _row_to_notification(row)


# =====================================================================
# Bulk mark all read (POST /mark-all-read)
# =====================================================================


_MARK_ALL_READ_SQL = """
UPDATE notifications
   SET read_at = now()
 WHERE read_at IS NULL
RETURNING 1
"""


@router.post(
    "/mark-all-read",
    response_model=NotificationMarkAllReadResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark every unread notification for the calling tenant as read",
)
async def mark_all_read(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> NotificationMarkAllReadResponse:
    """Bulk transition unread → read for the calling tenant.

    RLS scopes the UPDATE to the tenant's own rows; the WHERE clause
    here just narrows further to unread to avoid touching already-read
    rows (and to count the transition accurately via RETURNING).

    Idempotent: calling twice in succession returns marked_count=N the
    first time and 0 the second.
    """
    rows = await conn.fetch(_MARK_ALL_READ_SQL)
    return NotificationMarkAllReadResponse(marked_count=len(rows))
