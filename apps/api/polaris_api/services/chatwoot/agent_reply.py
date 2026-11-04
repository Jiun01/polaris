"""Persist human-agent Chatwoot replies into Polaris ``messages`` (phase-10).

Post-2026-06-13 Chatwoot-native cutover, a human agent's reply is sent to the
customer NATIVELY by Chatwoot — Polaris only acks the agent-bot webhook (200,
no re-dispatch). That left the portal Conversations view (backed by Polaris's
own ``messages`` table) showing customer inbounds + bot replies but NONE of the
human agent's responses on a handed-off conversation.

This module closes that gap for the bot-provider channels (whatsapp / messenger
/ telegram / instagram_login): when a genuine human agent (``message_type=
'outgoing'`` + ``sender.type='agent'``) replies, mirror that message into
``messages`` so it appears in the transcript.

Boundaries (mirrors the rest of the agent-reply path):
  * Echo-drop is enforced UPSTREAM in the router — ``human_agent_forward_inbox_id``
    only returns an inbox for ``sender.type=='agent'``; the bot's own mirror-posts
    (``sender.type=='agent_bot'``) and private notes (``message_type=='activity'``)
    never reach here. We NEVER re-dispatch to the provider.
  * Idempotent: the row is keyed by ``provider_message_id='chatwoot.<id>'`` (same
    convention as inbound) against the ``messages`` partial-unique index, so an
    agent-bot webhook retry is an ``ON CONFLICT DO NOTHING`` no-op.
  * Attribution: persisted as ``role='assistant'`` (it is an outbound, org-side
    turn — correct for the transcript AND for the bot's conversation-history
    window if the conversation later resumes) but tagged in ``metadata`` with
    ``author_type='human_agent'`` + the agent's name/id so the portal renders it
    distinctly from a bot-authored reply. No ``messages.role`` CHECK change (and
    so no migration) is needed — the existing ``metadata`` JSONB column already
    carries the distinction and ``polaris_anon`` already holds INSERT on it
    (migration 029).
  * Writes go through the ANON pool under RLS (``app.current_tenant_id`` GUC),
    exactly like the pipeline's own message writes — never the BYPASSRLS pool.

Conversation linkage: the Polaris ``conversations`` row already exists (created
+ linked to its Chatwoot conversation on the first bot-processed inbound, see
``pipeline._link_cw_conversation``). We look it up by
``(tenant_id, chatwoot_conversation_id)``. If none exists — the rare "human
replied before any bot turn" case — we log + skip rather than fabricate a
conversation (we'd have to re-resolve the customer's channel id, out of scope;
the next bot turn would create it).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import redis.asyncio as redis_async

from polaris_api.db.pools import get_anon_pool
from polaris_api.services import conversation as conversation_service

logger = logging.getLogger(__name__)

# Content stored when a human agent's reply is attachment-only (no text). The
# ``messages.content`` column is NOT NULL, so a placeholder keeps the row valid
# while the attachment refs ride in ``metadata.attachments`` for the portal.
_ATTACHMENT_PLACEHOLDER: Final[str] = "[Attachment]"

# Chatwoot file_type strings we recognise for the attachment summary (a subset
# kept human-readable; unknown types are dropped). Mirrors the router's map but
# kept local so this service never imports the webhook router (one-way dep).
_KNOWN_FILE_TYPES: Final[frozenset[str]] = frozenset(
    {"image", "audio", "video", "file", "story_mention", "ig_reel", "share"}
)


@dataclass(slots=True)
class HumanAgentReplyRow:
    """The persisted shape of one human-agent reply, derived purely from the
    Chatwoot ``message_created`` payload (no I/O — unit-testable)."""

    external_id: str
    chatwoot_message_id: int
    content: str
    created_at: datetime | None
    metadata: dict[str, Any] = field(default_factory=dict)


def _summarize_attachments(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Compact ``[{kind, url}]`` summary of a payload's attachments for metadata.

    Only ``http(s)`` urls are kept (an SSRF-shaped url is dropped, matching the
    router's inbound guard); unknown ``file_type`` values are skipped."""
    raw = payload.get("attachments")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        file_type = a.get("file_type")
        if not isinstance(file_type, str) or file_type not in _KNOWN_FILE_TYPES:
            continue
        entry: dict[str, str] = {"kind": file_type}
        data_url = a.get("data_url")
        if isinstance(data_url, str) and data_url.lower().startswith(
            ("http://", "https://")
        ):
            entry["url"] = data_url
        out.append(entry)
    return out


def _parse_created_at(raw: Any) -> datetime | None:
    """Parse Chatwoot's ISO ``created_at`` so the mirrored row orders correctly
    in the transcript relative to customer messages. ``None`` on absent/bad
    input → the INSERT falls back to ``now()``."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_reply_row(payload: Any) -> HumanAgentReplyRow | None:
    """Build the persisted row from a human-agent ``message_created`` payload.

    Pure (no I/O). Returns ``None`` when the payload can't be persisted:
      * not a dict, or missing/invalid top-level ``id`` (the Chatwoot message id);
      * no text AND no recognised attachment (nothing to show in the transcript).

    Caller (the router) has already enforced the echo-drop invariant via
    ``human_agent_forward_inbox_id`` — this function does NOT re-check sender
    type; it assumes a genuine human-agent reply.
    """
    if not isinstance(payload, dict):
        return None
    msg_id = payload.get("id")
    if not isinstance(msg_id, int):
        return None

    content_raw = payload.get("content")
    text = content_raw.strip() if isinstance(content_raw, str) else ""
    attachments = _summarize_attachments(payload)
    if not text and not attachments:
        return None

    sender = payload.get("sender")
    agent_name = sender.get("name") if isinstance(sender, dict) else None
    agent_id = sender.get("id") if isinstance(sender, dict) else None

    metadata: dict[str, Any] = {
        "author_type": "human_agent",
        "agent_name": agent_name if isinstance(agent_name, str) else None,
        "agent_id": agent_id if isinstance(agent_id, int) else None,
    }
    if attachments:
        metadata["attachments"] = attachments

    return HumanAgentReplyRow(
        external_id=f"chatwoot.{msg_id}",
        chatwoot_message_id=msg_id,
        content=text or _ATTACHMENT_PLACEHOLDER,
        created_at=_parse_created_at(payload.get("created_at")),
        metadata=metadata,
    )


async def persist_human_agent_reply(
    *,
    tenant_id: UUID,
    chatwoot_conversation_id: int,
    payload: dict[str, Any],
    redis: redis_async.Redis | None = None,
) -> bool:
    """Mirror one human-agent Chatwoot reply into Polaris ``messages``.

    Returns ``True`` when a new row was inserted, ``False`` when there was
    nothing to persist, the linked Polaris conversation does not exist yet, or
    the row already existed (idempotent retry).

    Raises ``asyncpg.PostgresError`` on a genuine DB outage — the router maps
    that to HTTP 503 so Chatwoot retries; the insert is idempotent so the retry
    is safe (no double-persist, no double-send — we never dispatch).
    """
    row = build_reply_row(payload)
    if row is None:
        return False

    pool = get_anon_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )
            conversation_id = await conn.fetchval(
                """
                SELECT id FROM conversations
                 WHERE tenant_id = $1 AND chatwoot_conversation_id = $2
                """,
                tenant_id,
                chatwoot_conversation_id,
            )
            if conversation_id is None:
                # Human replied before any bot turn linked a Polaris
                # conversation — nothing to attach to. Skip rather than
                # fabricate (we'd have to re-resolve the customer channel id).
                logger.warning(
                    "agent_reply persist: no Polaris conversation for "
                    "cw_conv=%s tenant=%s — reply not mirrored",
                    chatwoot_conversation_id,
                    tenant_id,
                )
                return False

            inserted = await conn.fetchrow(
                """
                INSERT INTO messages (
                    tenant_id, conversation_id, role, content,
                    provider_message_id, chatwoot_message_id, metadata, created_at
                ) VALUES (
                    $1, $2, 'assistant', $3,
                    $4, $5, $6, COALESCE($7, now())
                )
                ON CONFLICT (tenant_id, provider_message_id)
                    WHERE provider_message_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                tenant_id,
                conversation_id,
                row.content,
                row.external_id,
                row.chatwoot_message_id,
                # Pass the dict DIRECTLY — the anon pool registers a jsonb codec
                # (db/pools._init_conn) that serializes it once. Passing
                # json.dumps(...) + a ``::jsonb`` cast instead double-encodes
                # (the codec re-dumps the string → stored as a JSON string, not
                # an object). Verified against Postgres.
                row.metadata,
                row.created_at,
            )
            if inserted is None:
                # Conflict → an earlier webhook delivery already persisted this
                # exact Chatwoot message. Idempotent no-op.
                logger.debug(
                    "agent_reply persist: %s already mirrored (replay) tenant=%s",
                    row.external_id,
                    tenant_id,
                )
                return False

            # Surface the human reply as the conversation's latest activity so
            # the portal list sorts it to the top (same bump the pipeline does).
            await conn.execute(
                """
                UPDATE conversations SET last_message_at = now()
                 WHERE id = $1 AND tenant_id = $2
                """,
                conversation_id,
                tenant_id,
            )

    logger.info(
        "agent_reply persist: mirrored %s tenant=%s cw_conv=%s",
        row.external_id,
        tenant_id,
        chatwoot_conversation_id,
    )

    # Best-effort cache invalidation: the conv history cache (Redis) may hold a
    # pre-reply snapshot. Drop the key (via the owner of the cache-key schema)
    # so a resumed bot turn rebuilds history from Postgres WITH this agent
    # reply, rather than serving a stale window.
    if redis is not None:
        await conversation_service.invalidate_cache(
            redis, tenant_id, conversation_id
        )

    return True
