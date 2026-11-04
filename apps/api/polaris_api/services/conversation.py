"""Conversation history service — Phase 4 PR-3.

Two-tier storage for assistant ↔ user conversation turns:

  * **Supabase (durable)** — every message persists to the
    ``messages`` table (migration 028). RLS scopes reads + writes by
    ``app.current_tenant_id``; cross-tenant pollution is structurally
    impossible.

  * **Redis (hot cache)** — most-recent N turns per conversation
    cached under ``conv:{tenant_id}:{conversation_id}`` with 24h TTL.
    Hot path (``load_history`` on every inbound message) hits Redis
    first; only a miss / expiry rolls through to Postgres.

The dual-store posture matters for Phase 4 cost math: a 50-message
conversation hits the pipeline ~50 times. If every load_history did
a full ``SELECT`` from messages with sort + limit, that's 50× Postgres
reads per conversation per pricing window. Redis takes the reads to
~1 per 24h per conversation.

Cache invariants:
  - Key shape: ``conv:{tenant_id}:{conversation_id}`` — tenant_id
    FIRST so a code review reading the key knows which tenant owns
    it. cross-tenant leak via key reuse is structurally impossible.
  - Value: JSON list of ConversationMessage dicts, most-recent-last
    (matches DB natural order; loader trims from the end).
  - TTL: 24h; refreshed on every read AND write so an active
    conversation never expires from under itself.
  - Cap: 50 messages in cache. The loader's window-trim (default
    20 messages / 8k tokens) shrinks further for the LLM call; the
    50 in cache gives room for the trim algorithm to walk back over
    a couple of long turns before hitting Postgres.

Window-trim per ADR-031: walk most-recent-first, accumulate token
counts, stop at ``max_messages`` OR ``max_tokens``. The returned
``ConversationWindow`` carries ``truncated_by`` so the Phase 9
dashboard can chart how often each cap kicks in.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Final
from uuid import UUID

import asyncpg
import redis.asyncio as redis_async
from polaris_shared_types import (
    ConversationMessage,
    ConversationRole,
    ConversationWindow,
    TruncatedBy,
)

logger = logging.getLogger(__name__)

# Cache key shape — tenant_id FIRST so reviewers reading a key in
# `redis-cli KEYS conv:*` can immediately see which tenant owns it.
# Mirrors the inbound queue's namespace convention (Phase 2 PR-4).
_CACHE_KEY_PREFIX: Final[str] = "conv:"

# 24h TTL. Long enough to keep the typical multi-hour customer
# conversation hot; short enough that a stale cache after a Phase 9
# retention sweep self-heals within a day. Refreshed on every
# read + write so an active conversation never expires mid-session.
_CACHE_TTL_SECONDS: Final[int] = 86_400

# How many messages the cache holds at most. Loader window-trim
# shrinks further per call. Keeping ~50 in cache leaves room for the
# 20-message / 8k-token loader to walk back over a couple of long
# turns without falling back to Postgres.
_CACHE_MAX_MESSAGES: Final[int] = 50

# Token-count fallback heuristic when message.token_count is NULL.
# 4 chars/token is the canonical English approximation; CJK content
# under-counts ~1.5x, which is fine because under-counting is the
# safe direction (we'd keep MORE messages than needed, not fewer).
# Phase 9 dashboard surfaces actual ratio; revisit if eval shows the
# medium-tier conversation cap drifting.
_CHARS_PER_TOKEN_FALLBACK: Final[int] = 4


def _cache_key(tenant_id: UUID, conversation_id: UUID) -> str:
    return f"{_CACHE_KEY_PREFIX}{tenant_id}:{conversation_id}"


def _estimate_tokens(content: str, token_count: int | None) -> int:
    """Token count for window-trim; falls back to len/4 heuristic on NULL."""
    if token_count is not None:
        return token_count
    return max(1, len(content) // _CHARS_PER_TOKEN_FALLBACK)


def _serialize_messages(messages: list[ConversationMessage]) -> str:
    """JSON-encode a list of ConversationMessage for Redis storage.

    ``datetime`` → ISO-8601 string so the JSON round-trip is
    timezone-aware. Using ``model_dump_json`` per-item would force
    Pydantic to serialize the wrapping list, adding 50 chars per
    message in escape overhead; manual dict-build keeps the payload
    tight.
    """
    return json.dumps([
        {
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
            "token_count": m.token_count,
        }
        for m in messages
    ])


def _deserialize_messages(payload: str) -> list[ConversationMessage]:
    """Inverse of ``_serialize_messages``. Tolerates Pydantic-side
    field rename by validating each dict through ConversationMessage
    instead of passing **kwargs directly."""
    raw = json.loads(payload)
    return [
        ConversationMessage(
            role=item["role"],
            content=item["content"],
            created_at=datetime.fromisoformat(item["created_at"]),
            token_count=item.get("token_count"),
        )
        for item in raw
    ]


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


async def load_history(
    *,
    conn: asyncpg.Connection,
    redis: redis_async.Redis | None,
    tenant_id: UUID,
    conversation_id: UUID,
    max_messages: int = 20,
    max_tokens: int = 8000,
) -> ConversationWindow:
    """Load a windowed slice of recent messages, chronological order.

    Hot path: Redis (24h TTL). Cold path: ``SELECT`` from messages
    ordered DESC by created_at, then write the freshly-loaded slice
    back to Redis with refreshed TTL. Both paths apply window-trim
    before returning.

    Assumes the caller has set ``app.current_tenant_id`` GUC on the
    connection (PR-4 pipeline orchestrator does this). RLS double-
    guards against a spoofed ``tenant_id`` arg.

    ``redis`` may be None for tests that don't care about caching;
    the implementation just skips the cache layer in that case.
    """
    cache_messages: list[ConversationMessage] | None = None

    if redis is not None:
        cached = await redis.get(_cache_key(tenant_id, conversation_id))
        if cached is not None:
            payload = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            try:
                cache_messages = _deserialize_messages(payload)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                # Corrupt cache entry — log + treat as miss so the next
                # write rebuilds it cleanly. Never let a cache bug 5xx
                # the inbound pipeline.
                logger.warning(
                    "conv cache deserialize failed tenant=%s conv=%s err=%s",
                    tenant_id, conversation_id, type(exc).__name__,
                )
                cache_messages = None

    if cache_messages is None:
        cache_messages = await _load_from_db(
            conn, tenant_id, conversation_id, limit=_CACHE_MAX_MESSAGES,
        )
        if redis is not None and cache_messages:
            # Refresh + write-through. Best-effort: a Redis write
            # failure here is logged but does not propagate.
            try:
                await redis.set(
                    _cache_key(tenant_id, conversation_id),
                    _serialize_messages(cache_messages),
                    ex=_CACHE_TTL_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "conv cache rehydrate-write failed tenant=%s conv=%s err=%s",
                    tenant_id, conversation_id, type(exc).__name__,
                )

    if redis is not None and cache_messages:
        # Touch TTL on every read so an active conversation never
        # expires from under itself.
        try:
            await redis.expire(
                _cache_key(tenant_id, conversation_id), _CACHE_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("conv cache TTL refresh failed: %s", type(exc).__name__)

    return _apply_window(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        messages=cache_messages,
        max_messages=max_messages,
        max_tokens=max_tokens,
    )


async def save_message(
    *,
    conn: asyncpg.Connection,
    redis: redis_async.Redis | None,
    tenant_id: UUID,
    conversation_id: UUID,
    role: ConversationRole,
    content: str,
    token_count: int | None = None,
) -> UUID:
    """Append one message to the conversation. Returns the new row id.

    Order: Supabase first (durable). If the INSERT raises, Redis is
    untouched — the conversation cache stays consistent with the DB.
    If Redis fails AFTER a successful DB write, the cache is dropped
    (best-effort) and the next ``load_history`` rehydrates from
    Postgres. Logged but not raised: a Redis blip mid-conversation
    should never lose a successfully-persisted reply.

    Caller must have set ``app.current_tenant_id`` GUC for RLS.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO messages (tenant_id, conversation_id, role, content, token_count)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, created_at
        """,
        tenant_id, conversation_id, role, content, token_count,
    )
    if row is None:  # pragma: no cover — RETURNING always emits a row
        raise RuntimeError("save_message: INSERT returned no row")
    new_id: UUID = row["id"]
    created_at: datetime = row["created_at"]

    if redis is None:
        return new_id

    # Best-effort cache append. On any failure: drop the cache key
    # entirely so the next load_history rebuilds from DB instead of
    # serving a stale-but-not-corrupt entry.
    try:
        key = _cache_key(tenant_id, conversation_id)
        cached = await redis.get(key)
        if cached is None:
            messages: list[ConversationMessage] = []
        else:
            payload = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            try:
                messages = _deserialize_messages(payload)
            except (json.JSONDecodeError, KeyError, ValueError):
                messages = []
        messages.append(ConversationMessage(
            role=role, content=content, created_at=created_at,
            token_count=token_count,
        ))
        # Cap at _CACHE_MAX_MESSAGES; trim oldest first.
        if len(messages) > _CACHE_MAX_MESSAGES:
            messages = messages[-_CACHE_MAX_MESSAGES:]
        await redis.set(key, _serialize_messages(messages), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "conv cache append failed tenant=%s conv=%s err=%s — dropping cache",
            tenant_id, conversation_id, type(exc).__name__,
        )
        # Drop the (potentially stale) cache so the next read rebuilds.
        try:
            await redis.delete(_cache_key(tenant_id, conversation_id))
        except Exception:  # noqa: BLE001, S110
            pass  # If delete also fails, the next read will just see stale data.

    return new_id


async def invalidate_cache(
    redis: redis_async.Redis,
    tenant_id: UUID,
    conversation_id: UUID,
) -> None:
    """Best-effort drop of the conversation's hot cache key.

    For callers that wrote a turn to ``messages`` OUTSIDE this module's
    append helpers (e.g. ``services/chatwoot/agent_reply.py`` mirroring a
    human-agent reply) — dropping the key forces the next ``load_history``
    to rebuild the window from Postgres WITH the new turn, rather than
    serving a stale snapshot. Owns the cache-key schema so call sites don't
    re-derive the ``conv:`` literal. Never raises (mirrors ``save_message``'s
    drop-on-doubt posture)."""
    try:
        await redis.delete(_cache_key(tenant_id, conversation_id))
    except Exception:  # noqa: BLE001, S110 — cache drop is best-effort
        logger.debug(
            "conv cache invalidate failed tenant=%s conv=%s",
            tenant_id, conversation_id, exc_info=True,
        )


async def append_assistant_to_cache(
    *,
    redis: redis_async.Redis,
    tenant_id: UUID,
    conversation_id: UUID,
    content: str,
    created_at: datetime,
    token_count: int | None,
) -> None:
    """Best-effort cache append for an assistant turn already written to DB.

    Used by ``services/pipeline.py::_persist_assistant_message`` after it
    INSERTs the row with full telemetry (model_used, tokens, etc.) that
    ``save_message`` doesn't know about. Per code-review H-4: replaces
    the previous private-symbol cross-module imports from ``pipeline.py``
    with a single public seam owned by this module.

    Failure handling mirrors ``save_message``'s cache half — log the
    error, drop the cache key (so next read rebuilds from DB), never
    raise. The assistant message is ALREADY persisted to DB by the
    caller; a Redis blip mid-reply must not propagate.
    """
    key = _cache_key(tenant_id, conversation_id)
    try:
        cached = await redis.get(key)
        if cached is None:
            messages: list[ConversationMessage] = []
        else:
            payload = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            try:
                messages = _deserialize_messages(payload)
            except (json.JSONDecodeError, KeyError, ValueError):
                messages = []
        messages.append(ConversationMessage(
            role="assistant", content=content,
            created_at=created_at, token_count=token_count,
        ))
        if len(messages) > _CACHE_MAX_MESSAGES:
            messages = messages[-_CACHE_MAX_MESSAGES:]
        await redis.set(key, _serialize_messages(messages), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "append_assistant_to_cache failed tenant=%s conv=%s err=%s — dropping cache",
            tenant_id, conversation_id, type(exc).__name__,
        )
        try:
            await redis.delete(key)
        except Exception:  # noqa: BLE001, S110
            pass


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


async def _load_from_db(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    conversation_id: UUID,
    *,
    limit: int,
) -> list[ConversationMessage]:
    """Select most-recent N messages, return chronological (oldest first).

    L-2 audit fix (2026-05-19): exclude the ``[media-only message]``
    placeholder rows that ``pipeline._emit_handoff_for_empty_body``
    writes for media-only inbound (audio / image / document without
    caption). Keeping them in the LLM context window confuses the
    model — Sonnet has been observed echoing "I see you mentioned
    'media-only message' earlier". Phase 6 will replace the placeholder
    with structured media metadata; until then, we just drop the rows
    from history loads. They still exist in the DB for audit + the
    Phase 8 portal's conversation viewer.
    """
    rows = await conn.fetch(
        """
        SELECT role, content, created_at, token_count
          FROM messages
         WHERE tenant_id = $1
           AND conversation_id = $2
           AND content <> '[media-only message]'
         ORDER BY created_at DESC
         LIMIT $3
        """,
        tenant_id, conversation_id, limit,
    )
    # DB returns DESC; reverse to chronological so the rest of the
    # pipeline (Anthropic SDK messages parameter) gets oldest-first.
    return [
        ConversationMessage(
            role=row["role"],
            content=row["content"],
            created_at=row["created_at"],
            token_count=row["token_count"],
        )
        for row in reversed(rows)
    ]


def _apply_window(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    messages: list[ConversationMessage],
    max_messages: int,
    max_tokens: int,
) -> ConversationWindow:
    """Trim to last N OR M tokens, whichever ceiling hits first.

    Walks from the most-recent message backward. The ``truncated_by``
    field on the returned window lets the Phase 9 dashboard chart
    which ceiling is actually binding in production — drives any
    future tuning of the 20/8k defaults.
    """
    if not messages:
        return ConversationWindow(
            tenant_id=tenant_id, conversation_id=conversation_id,
            messages=[], truncated_by="none", total_tokens=0,
        )

    kept: list[ConversationMessage] = []
    running_tokens = 0
    truncated_by: TruncatedBy = "none"

    for msg in reversed(messages):
        tokens = _estimate_tokens(msg.content, msg.token_count)
        # Stop conditions evaluated BEFORE the message is added so we
        # don't include a message that would push us over the cap.
        if len(kept) >= max_messages:
            truncated_by = "message_count"
            break
        if kept and running_tokens + tokens > max_tokens:
            truncated_by = "token_count"
            break
        kept.append(msg)
        running_tokens += tokens

    # Loop break-conditions above already set truncated_by on every
    # reachable path that shrinks the input. Per code-review M-3: the
    # post-loop defensive branch was unreachable (kept.len < messages.len
    # implies the loop hit a stop condition, which set truncated_by).
    # Removed; the loop is the single source of truth for the flag.

    kept.reverse()  # chronological order out
    return ConversationWindow(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        messages=kept,
        truncated_by=truncated_by,
        total_tokens=running_tokens,
    )
