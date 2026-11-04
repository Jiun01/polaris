"""Opt-out viewer + mark_opted_out — Phase 6 PR-4.

Three public functions:

  * ``list_opted_out`` — paginated read for the portal viewer.
    Cursor-based; runs RLS-scoped against audience_members where
    opted_out_at IS NOT NULL.

  * ``stream_opted_out_csv`` — async generator yielding CSV lines for
    a ``StreamingResponse``. Keeps the API process memory bounded on
    100k-row tenants — emits header line, then per-row formatted CSV
    via ``csv.writer``.

  * ``mark_opted_out`` — single write path. Called by (a) STOP-keyword
    handler in inbound_worker (Phase 2 dep), (b) tenant_admin manual
    opt-out (Phase 8), (c) DSAR delete cascade (Phase 9). Idempotent:
    no-op + audit-row-marked-noop when a contact is already opted out.
    Emits the ``audience.opted_out`` outbound event row (persisted at
    GA, dispatched in v3.0 per ADR-035 Am evening).

Cursor format: base64(JSON({opted_out_at: ISO8601, id: UUID})). Stable
across inserts because the (opted_out_at, id) tuple is monotonic per
tenant. Forward-only.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from polaris_shared_types import OptOutReason, OptOutViewerPage, OptOutViewerRow

from polaris_api.services import audit
from polaris_api.services.chatwoot.client import get_chatwoot_client
from polaris_api.services.chatwoot.label_sync import sync_opt_out_label

logger = logging.getLogger(__name__)


def _encode_cursor(opted_out_at: datetime, row_id: UUID) -> str:
    """Encode a stable cursor for the next page."""
    payload = {"opted_out_at": opted_out_at.isoformat(), "id": str(row_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID] | None:
    """Decode a cursor; returns None on any malformed input.

    We don't raise on malformed cursors — a portal user pasting an
    expired or corrupted cursor gets the first page, which is the
    least-surprising fallback.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        opted_out_at = datetime.fromisoformat(payload["opted_out_at"])
        row_id = UUID(payload["id"])
        return opted_out_at, row_id
    except (ValueError, KeyError, TypeError):
        return None


async def list_opted_out(
    conn: asyncpg.Connection,
    *,
    channel: str | None = None,
    opted_out_since: datetime | None = None,
    cursor: str | None = None,
    page_size: int = 200,
) -> OptOutViewerPage:
    """Return one page of opted-out members under RLS.

    Filters:
      - ``channel``         optional exact match
      - ``opted_out_since`` optional lower-bound (>= timestamp)
      - ``cursor``          opaque base64 from a previous page's
                            ``next_cursor``; we ORDER BY
                            (opted_out_at DESC, id DESC) and the
                            cursor encodes the tuple of the last row
                            we returned.

    total_count is a SELECT COUNT(*) under the same WHERE — cheap
    because the audience_members_tenant_optout_idx partial index
    makes it an index-only scan.
    """
    # Page bounds — clamp to a sane envelope so a tenant requesting
    # page_size=999999 doesn't OOM us. Hard ceiling 1000.
    if page_size <= 0:
        page_size = 200
    if page_size > 1000:
        page_size = 1000

    where_clauses: list[str] = ["opted_out_at IS NOT NULL"]
    params: list[Any] = []
    if channel is not None:
        params.append(channel)
        where_clauses.append(f"channel = ${len(params)}")
    if opted_out_since is not None:
        params.append(opted_out_since)
        where_clauses.append(f"opted_out_at >= ${len(params)}")
    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if decoded is not None:
            cur_ts, cur_id = decoded
            params.append(cur_ts)
            params.append(cur_id)
            # Order DESC: we want rows STRICTLY BEFORE the cursor tuple.
            where_clauses.append(
                f"(opted_out_at, id) < (${len(params) - 1}, ${len(params)})"
            )

    where_sql = " AND ".join(where_clauses)

    # Fetch one extra row to know whether a next-cursor exists.
    fetch_n = page_size + 1
    params.append(fetch_n)

    # noqa: S608 — where_sql is composed from a fixed allowlist of clause
    # templates above; every interpolated $N is an asyncpg placeholder,
    # never a value. No injection vector.
    rows = await conn.fetch(
        f"""
        SELECT id, channel, channel_user_id, channel_user_name,
               phone_number_e164, locale, opted_out_at,
               opted_out_reason, opted_out_at_channel
          FROM audience_members
         WHERE {where_sql}
         ORDER BY opted_out_at DESC, id DESC
         LIMIT ${len(params)}
        """,  # noqa: S608
        *params,
    )

    has_more = len(rows) > page_size
    page_rows = list(rows[:page_size])

    # Total count under the same WHERE (drop the LIMIT param).
    count_params = params[:-1]
    if cursor is not None and _decode_cursor(cursor) is not None:
        # The cursor adds a (opted_out_at, id) < (...) filter to the
        # main query; the count query should NOT include that — count
        # is "how many match the user-supplied filters in total",
        # independent of where the user is in pagination.
        count_params = count_params[:-2]
        count_where_clauses = [c for c in where_clauses if "(opted_out_at, id) <" not in c]
        count_where_sql = " AND ".join(count_where_clauses)
    else:
        count_where_sql = where_sql

    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM audience_members WHERE {count_where_sql}",  # noqa: S608 — same clause-allowlist invariant
        *count_params,
    )

    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last["opted_out_at"], last["id"])

    return OptOutViewerPage(
        rows=[
            OptOutViewerRow(
                id=r["id"],
                channel=r["channel"],
                channel_user_id=r["channel_user_id"],
                channel_user_name=r["channel_user_name"],
                phone_number_e164=r["phone_number_e164"],
                locale=r["locale"],
                opted_out_at=r["opted_out_at"],
                opted_out_reason=r["opted_out_reason"],
                opted_out_at_channel=r["opted_out_at_channel"],
            )
            for r in page_rows
        ],
        next_cursor=next_cursor,
        total_count=int(total or 0),
    )


_CSV_COLUMNS = [
    "id",
    "channel",
    "channel_user_id",
    "channel_user_name",
    "phone_number_e164",
    "locale",
    "opted_out_at",
    "opted_out_reason",
    "opted_out_at_channel",
]


async def stream_opted_out_csv(
    conn: asyncpg.Connection,
    *,
    channel: str | None = None,
    opted_out_since: datetime | None = None,
) -> AsyncIterator[str]:
    """Yield CSV lines for the opted-out list under RLS.

    Pagination handled internally — we iterate the table in chunks of
    1000 and emit each row immediately so the API process never
    buffers the full result set. The generator runs inside the
    caller's existing request transaction.
    """
    # Header line.
    yield ",".join(_CSV_COLUMNS) + "\n"

    chunk_size = 1000
    cursor_ts: datetime | None = None
    cursor_id: UUID | None = None

    while True:
        where_clauses: list[str] = ["opted_out_at IS NOT NULL"]
        params: list[Any] = []
        if channel is not None:
            params.append(channel)
            where_clauses.append(f"channel = ${len(params)}")
        if opted_out_since is not None:
            params.append(opted_out_since)
            where_clauses.append(f"opted_out_at >= ${len(params)}")
        if cursor_ts is not None and cursor_id is not None:
            params.append(cursor_ts)
            params.append(cursor_id)
            where_clauses.append(
                f"(opted_out_at, id) < (${len(params) - 1}, ${len(params)})"
            )
        params.append(chunk_size)

        rows = await conn.fetch(
            f"""
            SELECT id, channel, channel_user_id, channel_user_name,
                   phone_number_e164, locale, opted_out_at,
                   opted_out_reason, opted_out_at_channel
              FROM audience_members
             WHERE {' AND '.join(where_clauses)}
             ORDER BY opted_out_at DESC, id DESC
             LIMIT ${len(params)}
            """,  # noqa: S608 — where_clauses is a fixed-template allowlist; values are placeholders
            *params,
        )

        if not rows:
            return

        for row in rows:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    str(row["id"]),
                    row["channel"],
                    row["channel_user_id"],
                    row["channel_user_name"] or "",
                    row["phone_number_e164"] or "",
                    row["locale"] or "",
                    row["opted_out_at"].isoformat() if row["opted_out_at"] else "",
                    row["opted_out_reason"] or "",
                    row["opted_out_at_channel"] or "",
                ]
            )
            yield buf.getvalue()

        if len(rows) < chunk_size:
            return  # last chunk, no more to fetch

        last = rows[-1]
        cursor_ts = last["opted_out_at"]
        cursor_id = last["id"]


async def mark_opted_out(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    channel: str,
    channel_user_id: str,
    reason: OptOutReason,
    opted_out_at_channel: str | None,
    actor: UUID | None = None,
) -> bool:
    """Mark a contact opted out. Returns True iff a row was flipped.

    Idempotent: if the active row is already opted_out_at IS NOT NULL,
    we no-op and record an audit row with outcome='noop' so the
    audit trail still captures the action (someone tried, system was
    already there).

    Always writes:
      - audit row (action='audience.opt_out', outcome encoded in after)
      - outbound event row (event_type='audience.opted_out') in
        webhook_events_outbound — persisted at GA, dispatched v3.0.
    """
    updated = await conn.execute(
        """
        UPDATE audience_members
           SET opted_out_at         = now(),
               opted_out_reason     = $4,
               opted_out_at_channel = $5,
               updated_at           = now()
         WHERE tenant_id      = $1
           AND channel        = $2
           AND channel_user_id = $3
           AND opted_out_at IS NULL
        """,
        tenant_id,
        channel,
        channel_user_id,
        reason,
        opted_out_at_channel,
    )

    flipped = not updated.endswith(" 0")
    outcome = "applied" if flipped else "noop"

    await audit.record(
        conn,
        tenant_id,
        actor,
        "audience.opt_out",
        before=None,
        after={
            "channel": channel,
            "channel_user_id": channel_user_id,
            "reason": reason,
            "opted_out_at_channel": opted_out_at_channel,
            "outcome": outcome,
        },
    )

    # Persist outbound event row (deferred dispatch per ADR-035 Am evening).
    # Guard against the table not existing in fixtures — same posture as
    # template lifecycle.
    try:
        await conn.execute(
            """
            INSERT INTO webhook_events_outbound
                (tenant_id, event_type, payload, created_at)
            VALUES ($1, $2, $3::jsonb, now())
            """,
            tenant_id,
            "audience.opted_out",
            json.dumps(
                {
                    "channel": channel,
                    "channel_user_id": channel_user_id,
                    "reason": reason,
                    "opted_out_at_channel": opted_out_at_channel,
                    "outcome": outcome,
                }
            ),
        )
    except asyncpg.UndefinedTableError:
        logger.warning(
            "audience.mark_opted_out: webhook_events_outbound missing; "
            "skipping outbound event persistence for tenant=%s",
            tenant_id,
        )

    # Phase 7 PR-4: fire Chatwoot label sync as a non-blocking side-effect.
    # The opt-out write above is the PDPA source of truth; the Chatwoot
    # label is best-effort eventual-consistency (≤5s SLA per Phase 7 exit
    # criteria). Any failure here MUST NOT raise — opt-out is regulated
    # work and we won't fail it on Chatwoot health.
    #
    # silent-failure-hunter LOW: imports promoted to module-level (top
    # of file) so structural import errors fail at app startup rather
    # than at the first STOP message in production. The try/except
    # below now guards ONLY runtime Chatwoot failures (the inner
    # sync_opt_out_label already catches its own ChatwootError; this
    # outer guard catches anything unexpected — e.g. asyncpg connection
    # mid-call drop).
    try:
        await sync_opt_out_label(
            tenant_id=tenant_id,
            channel=channel,
            channel_user_id=channel_user_id,
            conn=conn,
            chatwoot=get_chatwoot_client(),
        )
    except Exception:  # noqa: BLE001 — opt-out must never fail on Chatwoot
        logger.exception(
            "audience.mark_opted_out: label_sync hook crashed (non-blocking) "
            "tenant=%s channel=%s",
            tenant_id,
            channel,
        )

    return flipped
