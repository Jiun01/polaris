"""Tenant-facing read-only Conversations viewer — Phase 8 PR-6.

Mounted at ``/api/tenant/conversations`` per the standard prefix layering
(``/v1/webhooks/*`` + ``/api/tenant/*`` + ``/api/admin/*`` from
Q-PHASE3-06). Surfaces the portal Conversations screen (#2) — list +
detail. Reply UI lives in Chatwoot per ADR-030; no write surface here.

Two endpoints:

  * ``GET /``      — cursor-paginated list, newest first. Optional
                     ``channel`` filter (whatsapp | instagram |
                     messenger | demo) drives the portal's channel
                     chip filter. ``page_size`` capped at 50.
  * ``GET /{id}``  — full detail pane. Returns the conversation header
                     + every message ordered oldest-first so the portal
                     can render a top-to-bottom transcript without
                     client-side sort.

Auth: portal-JWT via ``get_tenant_context``. RLS on both ``conversations``
(migration 026) and ``messages`` (migration 028) scopes everything to the
calling tenant — the router does NOT redundantly filter on tenant_id, so
a misconfigured policy would surface as 404s in tests rather than as a
silent cross-tenant leak.

Pagination cursor mirrors ``tenant_notifications.py`` for consistency:
opaque ``<rfc3339>|<uuid>`` so equal-second bursts (rare on
conversations but possible during demo seeding) don't cause boundary
skips. Composite ordering ``(last_message_at DESC NULLS LAST, id DESC)``
matches the index in migration 026.

The list endpoint joins to ``messages`` for ``message_count`` +
``last_intent`` via two LATERAL subqueries — bounded by the page size
(≤50 outer rows) so the planner picks the per-conversation index instead
of a giant join.

Error envelope codes:

  * ``CONVERSATION_NOT_FOUND`` — id missing or RLS-stripped.
  * ``CONVERSATIONS_INVALID_CURSOR`` — malformed opaque cursor.
  * ``CONVERSATIONS_INVALID_CHANNEL`` — unsupported channel filter.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Annotated, get_args
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from polaris_shared_types.conversation_view import (
    ConversationChannel,
    ConversationDetail,
    ConversationListItem,
    ConversationListResponse,
    MessageAuthorType,
    MessageDetail,
)

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-conversations"])

_DEFAULT_PAGE_SIZE = 20
_DEFAULT_PAGE_SIZE_MAX = 50
_ALLOWED_CHANNELS: frozenset[str] = frozenset(get_args(ConversationChannel))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "env var %s=%r not int; using default %d", name, raw, default,
        )
        return default


_PAGE_SIZE_MAX: int = _env_int(
    "CONVERSATIONS_PAGE_SIZE_MAX", _DEFAULT_PAGE_SIZE_MAX,
)


def _encode_cursor(last_message_at: str, conversation_id: UUID) -> str:
    """Composite cursor: ``<rfc3339>|<uuid>``. Mirrors notifications router."""
    return f"{last_message_at}|{conversation_id}"


def _decode_cursor(token: str) -> tuple[datetime, UUID]:
    """Parse a cursor token. Raises ValueError on malformed input."""
    ts_part, _, id_part = token.partition("|")
    if not ts_part or not id_part:
        raise ValueError("cursor must be '<rfc3339>|<uuid>'")
    ts = datetime.fromisoformat(ts_part.replace("Z", "+00:00"))
    cid = UUID(id_part)
    return ts, cid


def _customer_display_name(
    metadata: dict[str, object] | None,
    channel_user_id: str,
) -> str:
    """Pull a stable display name for the customer.

    Pipeline writes ``metadata.display_name`` from the inbound webhook
    payload when available (WA push-name, IG/FB profile name). When
    absent, we redact ``channel_user_id`` to the last 4 chars so the
    portal still has a stable identifier without printing the full
    E.164 number / IG id in the list. The detail pane shows the
    full id since the operator is already drilled in.
    """
    if metadata:
        name = metadata.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    suffix = channel_user_id[-4:] if len(channel_user_id) >= 4 else channel_user_id
    return f"Customer …{suffix}"


def _author_type(metadata: dict[str, object] | None) -> MessageAuthorType:
    """Resolve a message's author — 'human_agent' for a human agent's Chatwoot
    reply mirrored into ``messages`` (``metadata.author_type``), else 'bot'.

    Closed mapping: only the exact ``'human_agent'`` marker maps through; every
    other value — empty/legacy metadata, or an unexpected future string — falls
    back to 'bot'. So the portal payload can only ever carry a valid
    ``MessageAuthorType`` (no unbounded metadata string leaks out), and the
    column is backward-compatible without a backfill.
    """
    if metadata and metadata.get("author_type") == "human_agent":
        return "human_agent"
    return "bot"


def _author_name(metadata: dict[str, object] | None) -> str | None:
    """The human agent's display name for a 'replied by' label, when present."""
    if metadata:
        name = metadata.get("agent_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


# =====================================================================
# List (GET /)
# =====================================================================

# The LATERAL subqueries surface message_count + last_intent per
# conversation without a full join. RLS on `messages` scopes both
# subqueries to the same tenant. ORDER BY in the lateral lookup picks
# the most recent message for `last_intent`.
_LIST_SQL = """
SELECT c.id,
       c.tenant_id,
       c.channel,
       c.channel_user_id,
       c.chatwoot_conversation_id,
       c.last_message_at,
       c.metadata,
       c.created_at,
       COALESCE(mc.n, 0)        AS message_count,
       li.intent                AS last_intent
  FROM conversations c
  LEFT JOIN LATERAL (
        SELECT COUNT(*) AS n
          FROM messages m
         WHERE m.conversation_id = c.id
  ) mc ON TRUE
  LEFT JOIN LATERAL (
        SELECT m.intent
          FROM messages m
         WHERE m.conversation_id = c.id
           AND m.intent IS NOT NULL
         ORDER BY m.created_at DESC
         LIMIT 1
  ) li ON TRUE
"""


@router.get(
    "/",
    response_model=ConversationListResponse,
    summary="List conversations for the calling tenant",
)
async def list_conversations(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    page_size: Annotated[int, Query(ge=1, le=_PAGE_SIZE_MAX)] = _DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    channel: Annotated[str | None, Query(max_length=16)] = None,
) -> ConversationListResponse:
    """List conversations, newest activity first.

    ``cursor``   — opaque pagination token from ``next_cursor``. Composite
                   ``(last_message_at, id)`` so equal-timestamp bursts
                   (demo seeding) don't cause boundary skips.
    ``channel``  — restrict to a specific channel. Validated against the
                   `conversations.channel` CHECK constraint before
                   reaching SQL — invalid values 400 with a clear code.

    Page size capped at ``CONVERSATIONS_PAGE_SIZE_MAX`` (default 50).
    """
    where_parts: list[str] = []
    args: list[object] = []

    if channel is not None:
        if channel not in _ALLOWED_CHANNELS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "CONVERSATIONS_INVALID_CHANNEL",
                    "message": (
                        f"channel must be one of {sorted(_ALLOWED_CHANNELS)}"
                    ),
                },
            )
        args.append(channel)
        where_parts.append(f"c.channel = ${len(args)}")

    if cursor is not None:
        try:
            cursor_dt, cursor_id = _decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "CONVERSATIONS_INVALID_CURSOR",
                    "message": (
                        "cursor must be an opaque token from a prior next_cursor"
                    ),
                },
            ) from exc
        # Row-value comparison matches ORDER BY (last_message_at, id) DESC.
        # NULL last_message_at sorts last on a DESC NULLS LAST index;
        # when the cursor's last_message_at is non-null, anything with a
        # NULL last_message_at is strictly after it, so the comparison
        # `(last_message_at, id) < (...)` correctly excludes them.
        args.append(cursor_dt)
        args.append(cursor_id)
        where_parts.append(
            f"(c.last_message_at, c.id) < (${len(args) - 1}, ${len(args)})"
        )

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    # +1 sentinel so we know whether to emit next_cursor without a
    # separate COUNT round-trip.
    args.append(page_size + 1)
    list_sql = (
        f"{_LIST_SQL}"
        f"{where_sql}\n"
        f"ORDER BY c.last_message_at DESC NULLS LAST, c.id DESC\n"
        f"LIMIT ${len(args)}"
    )
    rows = await conn.fetch(list_sql, *args)

    has_more = len(rows) > page_size
    if has_more:
        rows = rows[:page_size]

    items = [
        ConversationListItem(
            id=row["id"],
            tenant_id=row["tenant_id"],
            channel=row["channel"],
            channel_user_id=row["channel_user_id"],
            customer_display_name=_customer_display_name(
                row["metadata"], row["channel_user_id"],
            ),
            last_message_at=(
                row["last_message_at"].isoformat()
                if row["last_message_at"]
                else None
            ),
            last_intent=row["last_intent"],
            message_count=int(row["message_count"]),
            chatwoot_conversation_id=row["chatwoot_conversation_id"],
            created_at=row["created_at"].isoformat(),
        )
        for row in rows
    ]

    next_cursor: str | None = None
    if has_more and items:
        # Anchor on the last row's last_message_at; if that row's
        # last_message_at is NULL (rare — pre-message conversations
        # surfaced via cursor), fall back to its created_at so the
        # cursor still advances. Decode path requires a parseable RFC3339.
        tail = items[-1]
        anchor = tail.last_message_at or tail.created_at
        next_cursor = _encode_cursor(anchor, tail.id)

    return ConversationListResponse(items=items, next_cursor=next_cursor)


# =====================================================================
# Detail (GET /{conversation_id})
# =====================================================================


_DETAIL_HEADER_SQL = """
SELECT id,
       tenant_id,
       channel,
       channel_user_id,
       chatwoot_conversation_id,
       last_message_at,
       metadata,
       created_at
  FROM conversations
 WHERE id = $1
"""

# rag_chunk_ids is a UUID[] in postgres; COALESCE(cardinality(...),0)
# surfaces the count without hauling chunk bodies. The detail pane
# shows "Used N knowledge chunks" — exposing IDs is unnecessary noise.
_DETAIL_MESSAGES_SQL = """
SELECT id,
       role,
       content,
       intent,
       model_used,
       response_time_ms,
       input_tokens,
       output_tokens,
       cache_read_tokens,
       COALESCE(cardinality(rag_chunk_ids), 0) AS rag_chunk_count,
       metadata,
       created_at
  FROM messages
 WHERE conversation_id = $1
 ORDER BY created_at ASC,
          -- The user message + the assistant reply of one turn are persisted in
          -- the SAME pipeline transaction, so Postgres now() gives them an
          -- IDENTICAL created_at. id is a random UUID, so "id ASC" tie-broke the
          -- turn arbitrarily (showed human>bot>bot>human). Order user before
          -- assistant within a tied timestamp so each turn reads in real order.
          CASE role WHEN 'user' THEN 0 WHEN 'assistant' THEN 1 ELSE 2 END ASC,
          id ASC
"""


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    responses={
        404: {"description": "Conversation not visible to the calling tenant"},
    },
    summary="Fetch one conversation + its messages, oldest-first",
)
async def get_conversation(
    conversation_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> ConversationDetail:
    """Detail pane: full message history with bot telemetry.

    404 covers both "not found" and "another tenant's conversation"
    — RLS strips cross-tenant rows before the SELECT sees them, so we
    can't tell the two apart and shouldn't try (avoids cross-tenant
    existence leak).
    """
    header = await conn.fetchrow(_DETAIL_HEADER_SQL, conversation_id)
    if header is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "CONVERSATION_NOT_FOUND",
                "message": f"conversation {conversation_id} not found",
            },
        )

    message_rows = await conn.fetch(_DETAIL_MESSAGES_SQL, conversation_id)

    messages = [
        MessageDetail(
            id=row["id"],
            role=row["role"],
            content=row["content"],
            intent=row["intent"],
            model_used=row["model_used"],
            response_time_ms=row["response_time_ms"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            rag_chunk_count=int(row["rag_chunk_count"]),
            author_type=_author_type(row["metadata"]),
            author_name=_author_name(row["metadata"]),
            created_at=row["created_at"].isoformat(),
        )
        for row in message_rows
    ]

    return ConversationDetail(
        id=header["id"],
        tenant_id=header["tenant_id"],
        channel=header["channel"],
        channel_user_id=header["channel_user_id"],
        customer_display_name=_customer_display_name(
            header["metadata"], header["channel_user_id"],
        ),
        chatwoot_conversation_id=header["chatwoot_conversation_id"],
        last_message_at=(
            header["last_message_at"].isoformat()
            if header["last_message_at"]
            else None
        ),
        created_at=header["created_at"].isoformat(),
        messages=messages,
    )
