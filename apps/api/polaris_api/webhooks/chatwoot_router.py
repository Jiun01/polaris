"""POST /v1/webhooks/chatwoot — Chatwoot Agent-Bot loopback dispatcher.

Phase 2 PR-5 (initial) + PR-7 (gap fixes):

  PR-5 — HMAC verify → resolve_chatwoot → status-gate → SETNX claim →
         IncomingMessage emit + received row (text-only).
  PR-7 — Attachment extraction (A1); reply-context propagation (A4);
         DECISION_TO_EVENT_STATUS used consistently with the Meta router
         (D1); HTTP 503 on Redis / DB outage (B3/B4).

Chatwoot envelope (v4.13.0):

    {
      "event": "message_created",          // top-level discriminator
      "id": 12345,                          // chatwoot message id
      "account": {"id": 1},
      "inbox":   {"id": 7},                 // resolution key (UNIQUE in tenant_channels)
      "conversation": {"id": 99},
      "sender":  {"id": 42, "name": "Jane"},
      "content": "hello",
      "message_type": "incoming",           // we only process incoming
      "created_at": "2026-05-17T08:00:00Z",
      "attachments": [
        {"id":1, "file_type":"image", "data_url":"https://.../x.jpg",
         "thumb_url":"...", "file_size":12345}
      ],
      "content_attributes": {
        "in_reply_to":      {"in_reply_to_external_id":"chatwoot.42"},
        "in_reply_to_external_id": "wamid.abc"   // alternative location
      }
    }

Behaviour matrix::

    POST /v1/webhooks/chatwoot   missing/wrong sig                          → 401 + hmac_fail row
    POST /v1/webhooks/chatwoot   event != 'message_created'                 → 200 empty, no-op
    POST /v1/webhooks/chatwoot   message_type=outgoing + sender.type=agent  → 200; Chatwoot forwards to the customer natively. Bot-provider inbox → mirror into `messages` (portal transcript); email inbox → record draft acceptance only
    POST /v1/webhooks/chatwoot   message_type=outgoing + sender.type=agent_bot → 200 echo-avoidance no-op
    POST /v1/webhooks/chatwoot   message_type=activity or other non-incoming → 200 no-op
    POST /v1/webhooks/chatwoot   unknown inbox_id, lazy-provisioned         → continue (auto-creates tenant_channels on first inbound; entitled + whatsapp/messenger/instagram_login only)
    POST /v1/webhooks/chatwoot   unknown inbox_id, kind not entitled        → 204 + tenant_not_found (error_class=ChannelNotEntitled:<kind>) + negative cache
    POST /v1/webhooks/chatwoot   unknown inbox_id, no tenant / out-of-scope → 204 + tenant_not_found
    POST /v1/webhooks/chatwoot   suspended tenant           → 200 + suspended_* row, no enqueue
    POST /v1/webhooks/chatwoot   active + new id            → 200 + received  row + IncomingMessage emit
    POST /v1/webhooks/chatwoot   active + replay id         → 200 + replay_ignored row, no enqueue
    POST /v1/webhooks/chatwoot   incoming + human owns conv (status != 'pending') → 200 + human_handling row, no enqueue
    POST /v1/webhooks/chatwoot   incoming + unresolved recipient / non-bot channel → 200 + no_recipient row, no enqueue
    POST /v1/webhooks/chatwoot   Redis / DB outage          → 503 (Chatwoot retries)

Architectural rules — same as ``meta_router.py``: no BYPASSRLS pool
import here; all DB work goes through service modules that own the
BYPASSRLS service-pool acquisition — ``services/webhook_resolver.py``
(resolution + event rows) and ``services/channels/lazy_provision.py``
(first-inbound auto-provision). The router never touches the pool directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Final, cast

import asyncpg
import redis.exceptions as redis_exc
from fastapi import APIRouter, HTTPException, Request, Response, status
from polaris_shared_types import Attachment, IncomingMessage, Provider, SenderRef

from polaris_api.config import get_settings
from polaris_api.redis_pool import get_redis
from polaris_api.services import email_assist, email_assist_queue
from polaris_api.services.channels.lazy_provision import lazy_provision_channel
from polaris_api.services.chatwoot import agent_reply as agent_reply_service
from polaris_api.services.chatwoot.reverse_handoff import process_reverse_handoff
from polaris_api.services.idempotency import claim as idem_claim
from polaris_api.services.idempotency import release as idem_release
from polaris_api.services.inbound_queue import enqueue
from polaris_api.services.webhook_resolver import (
    record_event,
    resolve_by_chatwoot_account_id,
    resolve_chatwoot,
    resolve_chatwoot_with_status,
)
from polaris_api.webhooks._hmac import verify_chatwoot_signature
from polaris_api.webhooks._status_gate import (
    DECISION_TO_EVENT_STATUS,
    StatusDecision,
)
from polaris_api.webhooks._status_gate import (
    decide as decide_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

_CHATWOOT_SIGNATURE_HEADER = "x-chatwoot-signature"
_CHATWOOT_TIMESTAMP_HEADER = "x-chatwoot-timestamp"

# Lazy provisioning (first-inbound auto-provision): when a message arrives for an
# inbox whose kind is NOT in the tenant's allowed_channel_kinds, set a short
# negative-cache key so repeat messages don't re-hit list_inboxes / re-alert.
# The TTL bounds recovery latency once entitlements are widened.
_LAZY_SKIP_TTL_S: Final[int] = 600  # 10 min


def _lazy_skip_key(account_id: int, inbox_id: int) -> str:
    # Namespaced by account_id: Chatwoot inbox ids are per-account, so two
    # tenants could share an inbox_id on one deployment.
    return f"lazy_skip:{account_id}:{inbox_id}"


def _backend_outage_exc(where: str, exc: Exception) -> HTTPException:
    """Log a backend (DB/Redis) outage and build the retry-triggering 500.

    Returns 500 (not 503) so the Chatwoot agent-bot webhook RETRIES (500 is in
    RETRYABLE_AGENT_BOT_STATUSES regardless of the fork's 502/503/504 addition)
    instead of evicting the conversation from bot mode on a transient backend
    blip. ``where`` labels the failing stage in the log line.
    """
    logger.error(
        "chatwoot webhook: %s backend outage %s: %s",
        where,
        type(exc).__name__,
        exc,
    )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="backend_unavailable",
    )

# Chatwoot file_type → Attachment.kind. Anything not in this map gets
# skipped (Attachment.kind is a closed Literal in the SSOT). Chatwoot
# v4.13 uses these strings exactly per app/models/attachment.rb.
_CW_FILE_TYPE_TO_KIND: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
    "file": "document",
    "story_mention": "image",
    "ig_reel": "video",
    "share": "image",
}


def _extract_chatwoot_attachments(payload: dict[str, Any]) -> list[Attachment]:
    """Walk Chatwoot's ``attachments[]`` into the normalised Attachment shape."""
    out: list[Attachment] = []
    raw = payload.get("attachments")
    if not isinstance(raw, list):
        return out
    for a in raw:
        if not isinstance(a, dict):
            continue
        file_type = a.get("file_type")
        if not isinstance(file_type, str):
            continue
        kind = _CW_FILE_TYPE_TO_KIND.get(file_type)
        if kind is None:
            continue
        data_url = a.get("data_url")
        # SSRF guard: an agent-forwarded attachment URL is handed to Meta /
        # Telegram for a server-side fetch. Only accept http(s) URLs so a
        # forged payload can't smuggle file://, data:, or other schemes
        # through the bot. (Domain is not pinned — Chatwoot may serve
        # attachments from ActiveStorage/CDN hosts.)
        if isinstance(data_url, str) and not data_url.lower().startswith(
            ("http://", "https://")
        ):
            logger.warning(
                "chatwoot attachment data_url has non-http scheme — dropped"
            )
            data_url = None
        a_id = a.get("id")
        out.append(
            Attachment(
                kind=kind,  # type: ignore[arg-type]
                url=data_url if isinstance(data_url, str) else None,
                media_id=str(a_id) if isinstance(a_id, int) else None,
                mime_type=None,  # Chatwoot doesn't ship MIME in the webhook envelope
            )
        )
    return out


def _extract_chatwoot_reply_id(payload: dict[str, Any]) -> str | None:
    """Pull the provider-side id of the message being replied to.

    Chatwoot v4.13 puts this in one of two places depending on whether
    the reply was UI-initiated or relay-initiated:
      - ``content_attributes.in_reply_to``     — UI-initiated reply (numeric
        chatwoot message id); we stringify as ``chatwoot.<id>`` to match
        our provider_message_id convention.
      - ``content_attributes.in_reply_to_external_id`` — relay-initiated
        reply (the channel-native wamid / mid).

    Returns ``None`` when neither field is present.
    """
    ca = payload.get("content_attributes")
    if not isinstance(ca, dict):
        return None
    ext = ca.get("in_reply_to_external_id")
    if isinstance(ext, str) and ext:
        return ext
    cw_reply = ca.get("in_reply_to")
    if isinstance(cw_reply, int):
        return f"chatwoot.{cw_reply}"
    if isinstance(cw_reply, dict):
        inner_ext = cw_reply.get("in_reply_to_external_id")
        if isinstance(inner_ext, str) and inner_ext:
            return inner_ext
    return None


_HUMAN_HANDLING_LABEL: Final[str] = "human-handling"


def _human_handling_label_removed(payload: dict[str, Any]) -> bool:
    """True when a ``conversation_updated`` event shows the ``human-handling``
    label was just removed (a human agent finished → the bot may resume).

    The Polaris Chatwoot fork delivers label changes to the agent bot via
    ``conversation_updated`` with ``changed_attributes`` as a list of single-key
    objects, e.g.::

        [{"label_list": {"previous_value": ["human-handling"], "current_value": []}}]

    (agent_bot_listener#conversation_updated + base_listener#extract_changed_attributes).
    Stock Chatwoot emits no ``label_updated`` event, so this ``label_list`` delta
    on ``conversation_updated`` is the only label-removal signal available.
    """
    changed = payload.get("changed_attributes")
    if not isinstance(changed, list):
        return False
    for entry in changed:
        if not isinstance(entry, dict):
            continue
        label_change = entry.get("label_list")
        if not isinstance(label_change, dict):
            continue
        prev = label_change.get("previous_value")
        curr = label_change.get("current_value")
        prev_labels = set(prev) if isinstance(prev, list) else set()
        curr_labels = set(curr) if isinstance(curr, list) else set()
        return (
            _HUMAN_HANDLING_LABEL in prev_labels
            and _HUMAN_HANDLING_LABEL not in curr_labels
        )
    return False


def _should_resume(event: str, payload: dict[str, Any]) -> bool:
    """Return True when this event means a human agent finished and the bot should resume."""
    if event == "conversation_status_changed":
        conv = payload.get("conversation")
        return isinstance(conv, dict) and conv.get("status") == "resolved"
    if event == "conversation_updated":
        # Human agent removed the 'human-handling' label → resume the bot.
        return _human_handling_label_removed(payload)
    return False


# --- Chatwoot-native consumer (rehaul Stage 1) --------------------------------
# Chatwoot is the channel front-door now: the agent-bot fires message_created on
# EVERY inbound regardless of who owns the conversation, and the real reply
# recipient (the customer's provider-side id) lives on the conversation — not on
# provider='chatwoot'. These pure helpers (a) gate the bot to bot-owned
# conversations and (b) resolve the real channel + recipient so pipeline.run
# replies via Meta to the right person.

_BOT_OWNED_STATUS: Final[str] = "pending"
_BOT_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"whatsapp", "messenger", "telegram", "instagram_login"}
)


def is_bot_owned(conversation: Any) -> bool:
    """True iff the bot should process this message — the Chatwoot conversation
    is ``pending`` (bot-owned). ``open`` / ``resolved`` / ``snoozed`` mean a
    human owns it; the bot stays silent (never reply over a human). Status is
    the sole gate: a pending conversation may still carry a human assignee —
    status wins. Missing / malformed conversation → not bot-owned (fail safe)."""
    return (
        isinstance(conversation, dict)
        and conversation.get("status") == _BOT_OWNED_STATUS
    )


def human_agent_forward_inbox_id(payload: Any) -> int | None:
    """Echo-drop invariant (rehaul Stage 2). Return the inbox id to forward to
    iff this is a *human agent's* outgoing reply (``message_type='outgoing'`` +
    ``sender.type='agent'``); otherwise ``None``.

    A ``None`` return is the echo-drop: the bot's OWN reply mirror-posts arrive
    as ``outgoing`` + ``sender.type='agent_bot'`` and MUST NOT be re-forwarded
    to the customer (double-send) nor re-processed. Private notes
    (``message_type='activity'``) and every other non-human-agent event also
    return ``None``. Malformed payload / missing inbox id → ``None`` (fail safe
    — never echo)."""
    if not isinstance(payload, dict) or payload.get("message_type") != "outgoing":
        return None
    sender = payload.get("sender")
    sender_type = sender.get("type") if isinstance(sender, dict) else None
    if sender_type != "agent":
        return None
    inbox = payload.get("inbox")
    inbox_id = inbox.get("id") if isinstance(inbox, dict) else None
    # Chatwoot inbox ids are >=1; reject 0 / negative (falsy-but-int) so a
    # malformed payload can't dispatch a forward to a non-existent inbox.
    return inbox_id if isinstance(inbox_id, int) and inbox_id > 0 else None


def _strip_recipient(value: str, channel_kind: str) -> str:
    """Normalise a recipient id to the channel-native form reply_dispatch
    expects: WhatsApp = E.164 digits without '+'; identifiers may arrive as the
    legacy ``"<channel>:<id>"`` contact key — strip that prefix."""
    v = value.strip()
    if channel_kind == "whatsapp":
        return v.removeprefix("+")
    prefix = f"{channel_kind}:"
    return v[len(prefix) :] if v.startswith(prefix) else v


def extract_chatwoot_recipient(conversation: Any, channel_kind: str) -> str | None:
    """Resolve the REAL reply recipient (provider-side user id) from the
    agent-bot payload's ``conversation`` object.

    Chatwoot's native channel stores it on ``contact_inbox.source_id`` (WA
    wa_id, IG/Messenger PSID, Telegram chat id) — the canonical source. Falls
    back to the contact's ``phone_number`` (WA) / ``identifier`` under
    ``meta.sender``. Returns ``None`` when nothing resolves (no reply is
    possible → caller skips)."""
    if not isinstance(conversation, dict):
        return None
    ci = conversation.get("contact_inbox")
    if isinstance(ci, dict):
        sid = ci.get("source_id")
        if isinstance(sid, str) and sid.strip():
            return _strip_recipient(sid, channel_kind)
    meta = conversation.get("meta")
    sender = meta.get("sender") if isinstance(meta, dict) else None
    if isinstance(sender, dict):
        if channel_kind == "whatsapp":
            phone = sender.get("phone_number")
            if isinstance(phone, str) and phone.strip():
                return _strip_recipient(phone, channel_kind)
        ident = sender.get("identifier")
        if isinstance(ident, str) and ident.strip():
            return _strip_recipient(ident, channel_kind)
    return None


def channel_kind_to_provider(channel_kind: str) -> Provider:
    """Map a resolved ``tenant_channels.channel`` kind to the IncomingMessage
    Provider for the bot pipeline. Email is handled on its own draft branch
    upstream; 'chatwoot' is never a customer channel — both raise."""
    if channel_kind in _BOT_PROVIDERS:
        return cast(Provider, channel_kind)
    raise ValueError(f"channel_kind {channel_kind!r} is not a bot provider")


async def _dispatch_reverse_handoff(event: str, payload: dict[str, Any]) -> None:
    """Resolve tenant + conversation, then delegate to process_reverse_handoff."""
    account = payload.get("account")
    account_id = account.get("id") if isinstance(account, dict) else None
    conversation = payload.get("conversation")
    conversation_id = conversation.get("id") if isinstance(conversation, dict) else None

    if not isinstance(account_id, int) or not isinstance(conversation_id, int):
        logger.warning(
            "reverse_handoff: missing account.id or conversation.id in %s payload",
            event,
        )
        return

    resolved = await resolve_by_chatwoot_account_id(account_id)
    if resolved is None:
        logger.warning(
            "reverse_handoff: no tenant for chatwoot_account_id=%s", account_id
        )
        return

    trigger_detail = (
        "status=resolved"
        if event == "conversation_status_changed"
        else "human-handling label removed"
    )
    await process_reverse_handoff(
        tenant_id=resolved.tenant_id,
        chatwoot_account_id=account_id,
        chatwoot_conversation_id=conversation_id,
        trigger_event=event,
        trigger_detail=trigger_detail,
    )


async def _handle_human_agent_reply(inbox_id: int, payload: dict[str, Any]) -> None:
    """Route a genuine human-agent reply (phase-10 cutover).

    Chatwoot is the channel front-door now and forwards human-agent replies to
    the customer NATIVELY, so Polaris never re-sends them (the old
    ``_dispatch_agent_forward`` path is gone — it double-sent on Messenger /
    Instagram). What Polaris DOES still own, by channel kind:

      * **bot-provider** (whatsapp / messenger / telegram / instagram_login) —
        mirror the reply into Polaris's own ``messages`` table so it shows in the
        portal Conversations transcript. Before this, a handed-off conversation
        showed customer inbounds + bot replies but NONE of the human agent's
        responses. Idempotent (keyed on the Chatwoot message id), and NEVER
        re-dispatched to the provider.
      * **email** — score the reply text against the pending draft (email channel
        M2, PRD RD #15). Chatwoot's own email inbox delivered the actual message;
        we only record the acceptance outcome. The email transcript lives in
        Chatwoot, not the portal, so it is NOT mirrored into ``messages``.

    The echo-drop invariant is already enforced by the caller — only a real human
    agent (``sender.type=='agent'``) reaches here; the bot's own mirror-posts
    (``agent_bot``) and private notes never do.

    Error posture: ``resolve_chatwoot`` and the bot-provider persist are left to
    propagate ``asyncpg.PostgresError`` so the caller's try/except maps a genuine
    DB outage to HTTP 503 (Chatwoot retries; both paths are idempotent). The
    email acceptance write is best-effort (swallowed) — a stale draft-score must
    not 503 and trigger a retry of the agent's already-sent message."""
    conversation = payload.get("conversation") or {}
    cw_conv_id = conversation.get("id") if isinstance(conversation, dict) else None
    if not isinstance(cw_conv_id, int):
        logger.debug("agent_reply: missing conversation.id — skip")
        return

    resolved = await resolve_chatwoot(inbox_id)
    if resolved is None:
        return

    if resolved.channel_kind in _BOT_PROVIDERS:
        await agent_reply_service.persist_human_agent_reply(
            tenant_id=resolved.tenant_id,
            chatwoot_conversation_id=cw_conv_id,
            payload=payload,
            redis=get_redis(),
        )
        return

    if resolved.channel_kind == "email":
        content = payload.get("content")
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            # Attachment-only reply → nothing to score against the draft.
            return
        try:
            await email_assist.record_agent_reply(
                tenant_id=resolved.tenant_id,
                chatwoot_conversation_id=cw_conv_id,
                agent_reply_text=text,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "agent_reply: email acceptance tracking failed cw_conv=%s",
                cw_conv_id,
                exc_info=True,
            )


@router.post(
    "/chatwoot",
    summary="Chatwoot Agent-Bot loopback webhook",
    description=(
        "Receives Chatwoot fork v4.13.0+ signed webhook events. PR-5 reacts "
        "to `message_created` for incoming messages only. Outbound + "
        "conversation-status events land in Phase 7 (handoff). PR-7 added "
        "media-attachment + reply-context extraction + 503-on-outage."
    ),
)
async def chatwoot_inbound(  # noqa: PLR0911, PLR0912 — branch matrix is explicit by design
    request: Request,
) -> Response:
    """Verify HMAC → parse → ignore non-incoming → resolve → status-gate → enqueue."""
    raw: bytes = await request.body()
    header = request.headers.get(_CHATWOOT_SIGNATURE_HEADER)
    sig_timestamp = request.headers.get(_CHATWOOT_TIMESTAMP_HEADER)
    if sig_timestamp is None:
        logger.warning(
            "chatwoot webhook: %s header absent — replay protection disabled "
            "(legacy HMAC path)",
            _CHATWOOT_TIMESTAMP_HEADER,
        )
    secret = get_settings().chatwoot_webhook_hmac_secret
    payload_hash = hashlib.sha256(raw).digest()
    payload_hash_hex = payload_hash.hex()
    received_at = datetime.now(UTC)

    if not verify_chatwoot_signature(raw, header, secret, timestamp=sig_timestamp):
        logger.warning("chatwoot webhook HMAC verify failed")
        try:
            await record_event(
                source="chatwoot",
                external_id=payload_hash_hex,
                status="hmac_fail",
                payload_hash=payload_hash,
                error_class="HMACVerifyFailed",
            )
        except (asyncpg.PostgresError, redis_exc.RedisError) as exc:
            logger.error(
                "chatwoot webhook: record_event during hmac_fail failed: %s",
                type(exc).__name__,
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    payload: Any
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("chatwoot webhook body authenticated but not valid JSON")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if not isinstance(payload, dict):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    event = payload.get("event")

    if event in ("conversation_status_changed", "conversation_updated"):
        if _should_resume(event, payload):
            try:
                await _dispatch_reverse_handoff(event, payload)
            except (asyncpg.PostgresError, redis_exc.RedisError) as exc:
                raise _backend_outage_exc("reverse_handoff", exc) from exc
        return Response(status_code=status.HTTP_200_OK)

    if event != "message_created":
        logger.debug("chatwoot webhook ignored: event=%r", event)
        return Response(status_code=status.HTTP_200_OK)

    message_type = payload.get("message_type")
    if message_type != "incoming":
        # Non-incoming. Post-cutover, Chatwoot forwards human-agent replies to
        # the customer NATIVELY — Polaris must NOT re-send them (that double-sent
        # on Messenger / Instagram). What we DO on a genuine human-agent reply
        # (outgoing + sender.type='agent'): mirror it into Polaris's `messages`
        # table on a bot-provider inbox so the portal transcript shows it, or
        # score the pending draft on an email inbox. The bot's own reply
        # mirror-posts (outgoing + sender.type='agent_bot') and private notes
        # (message_type='activity') return None from
        # human_agent_forward_inbox_id() — the echo-drop invariant — and fall
        # straight through to 200.
        fwd_inbox_id = human_agent_forward_inbox_id(payload)
        if fwd_inbox_id is not None:
            try:
                await _handle_human_agent_reply(fwd_inbox_id, payload)
            except (asyncpg.PostgresError, redis_exc.RedisError) as exc:
                raise _backend_outage_exc("agent_reply", exc) from exc
        return Response(status_code=status.HTTP_200_OK)

    inbox = payload.get("inbox")
    inbox_id = inbox.get("id") if isinstance(inbox, dict) else None

    try:
        if not isinstance(inbox_id, int):
            logger.warning("chatwoot webhook missing/invalid inbox.id")
            await record_event(
                source="chatwoot",
                external_id=payload_hash_hex,
                status="tenant_not_found",
                payload_hash=payload_hash,
                error_class="MissingInboxId",
            )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        # One round-trip: resolve the channel row + the owning tenant's status
        # together (was two sequential service-pool acquires). Trims latency on
        # the agent-bot hot path so a slow pooler is less likely to push us past
        # Chatwoot's webhook timeout (which would evict the conversation).
        resolved, status_str, suspended_reason = await resolve_chatwoot_with_status(
            inbox_id
        )
        if resolved is None:
            # Lazy provisioning: a first inbound on an unregistered inbox
            # auto-creates the tenant_channels row using the Chatwoot service
            # token, RESPECTING allowed_channel_kinds. A short negative cache
            # stops repeat messages from a disallowed inbox re-hitting
            # list_inboxes / re-alerting; it expires so a later entitlement
            # widen self-heals. account.id is present on every Chatwoot envelope.
            account = payload.get("account")
            account_id = account.get("id") if isinstance(account, dict) else None
            redis = get_redis()
            if (
                isinstance(account_id, int)
                and await redis.get(_lazy_skip_key(account_id, inbox_id)) is None
            ):
                lazy = await lazy_provision_channel(
                    account_id=account_id, inbox_id=inbox_id
                )
                if lazy.outcome == "provisioned" and lazy.resolved is not None:
                    resolved = lazy.resolved
                    status_str = lazy.tenant_status
                    suspended_reason = lazy.suspended_reason
                    logger.info(
                        "chatwoot webhook: lazy-provisioned inbox_id=%s kind=%s",
                        inbox_id,
                        lazy.channel_kind,
                    )
                elif lazy.outcome == "disallowed":
                    await redis.set(
                        _lazy_skip_key(account_id, inbox_id),
                        b"1",
                        ex=_LAZY_SKIP_TTL_S,
                    )
                    # No 'channel_not_allowed' status exists (would need a
                    # migration); reuse tenant_not_found + a distinguishing
                    # error_class so it still surfaces on the health page.
                    await record_event(
                        source="chatwoot",
                        external_id=f"{payload_hash_hex[:16]}:inbox:{inbox_id}",
                        status="tenant_not_found",
                        payload_hash=payload_hash,
                        error_class=f"ChannelNotEntitled:{lazy.channel_kind}",
                    )
                    logger.warning(
                        "chatwoot webhook channel_not_allowed: inbox_id=%s kind=%s",
                        inbox_id,
                        lazy.channel_kind,
                    )
                    return Response(status_code=status.HTTP_204_NO_CONTENT)
                # no_tenant / not_applicable → fall through to tenant_not_found

        if resolved is None:
            await record_event(
                source="chatwoot",
                external_id=f"{payload_hash_hex[:16]}:inbox:{inbox_id}",
                status="tenant_not_found",
                payload_hash=payload_hash,
                error_class="NoChannelMatch:chatwoot",
            )
            logger.warning("chatwoot webhook tenant_not_found: inbox_id=%s", inbox_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        decision = decide_status(status_str, suspended_reason)

        if decision is not StatusDecision.PROCEED:
            await record_event(
                source="chatwoot",
                external_id=f"{payload_hash_hex[:16]}:inbox:{inbox_id}",
                status=DECISION_TO_EVENT_STATUS[decision],  # type: ignore[arg-type]
                payload_hash=payload_hash,
                tenant_id=resolved.tenant_id,
                channel_id=resolved.channel_id,
            )
            logger.info(
                "chatwoot webhook suspended-branch taken: tenant=%s decision=%s reason=%s",
                resolved.tenant_id,
                decision.value,
                suspended_reason,
            )
            return Response(status_code=status.HTTP_200_OK)

        # PROCEED: per-message idempotency + enqueue.
        chatwoot_message_id = payload.get("id")
        if not isinstance(chatwoot_message_id, int):
            logger.warning("chatwoot webhook missing/invalid id")
            return Response(status_code=status.HTTP_200_OK)

        external_id = f"chatwoot.{chatwoot_message_id}"

        # --- Human-handling gate (rehaul Stage 1) --------------------------
        # The agent-bot fires on EVERY inbound regardless of who owns the
        # conversation. Process ONLY when Chatwoot says it is bot-owned
        # (status='pending'); open/resolved/snoozed mean a human is handling it
        # — stay silent (never reply over a human). Runs BEFORE idem_claim so
        # the majority human-owned path skips the dedup slot + email/recipient
        # work. Email is exempt — it always drafts regardless of status.
        conversation = payload.get("conversation")
        if resolved.channel_kind != "email" and not is_bot_owned(conversation):
            await record_event(
                source="chatwoot",
                external_id=external_id,
                status="human_handling",
                payload_hash=payload_hash,
                tenant_id=resolved.tenant_id,
                channel_id=resolved.channel_id,
            )
            return Response(status_code=status.HTTP_200_OK)

        redis = get_redis()
        is_new = await idem_claim(redis, "chatwoot", external_id)

        if not is_new:
            await record_event(
                source="chatwoot",
                external_id=external_id,
                status="replay_ignored",
                payload_hash=payload_hash,
                tenant_id=resolved.tenant_id,
                channel_id=resolved.channel_id,
            )
            return Response(status_code=status.HTTP_200_OK)

        if resolved.channel_kind == "email":
            # Chatwoot-native email inbox → bot-assist draft (email channel M2).
            # Routed to the dedicated email-assist queue, NOT the generic inbound
            # queue (which dead-letters provider='chatwoot' at pipeline.py:157).
            job = email_assist.build_job(
                payload,
                tenant_id=resolved.tenant_id,
                channel_id=resolved.channel_id,
            )
            if job is None:
                logger.warning(
                    "chatwoot webhook: email payload missing account/conversation "
                    "id — no draft enqueued (inbox_id=%s)",
                    inbox_id,
                )
            elif not await email_assist_queue.enqueue(job):
                # Queue full → job dropped. The idempotency claim is already set,
                # so returning 200 here would make Chatwoot's retry dedupe as
                # replay_ignored = a customer email with no draft, silently lost.
                # Release the claim + 503 so the retry RE-processes (recoverable).
                await idem_release(redis, "chatwoot", external_id)
                logger.error(
                    "chatwoot webhook: email-assist queue full — released "
                    "idempotency claim + 503 for Chatwoot retry tenant=%s conv=%s",
                    resolved.tenant_id,
                    job.chatwoot_conversation_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="email_assist_backpressure",
                )
            await record_event(
                source="chatwoot",
                external_id=external_id,
                status=DECISION_TO_EVENT_STATUS[StatusDecision.PROCEED],  # type: ignore[arg-type]
                payload_hash=payload_hash,
                tenant_id=resolved.tenant_id,
                channel_id=resolved.channel_id,
            )
            return Response(status_code=status.HTTP_200_OK)

        # --- Resolve the REAL channel recipient ----------------------------
        # (The human-handling gate already ran before idem_claim above.)
        # Email is branched out earlier; any other non-bot channel kind is
        # skipped here rather than 500-ing the IncomingMessage build below.
        if resolved.channel_kind not in _BOT_PROVIDERS:
            await record_event(
                source="chatwoot",
                external_id=external_id,
                status="no_recipient",
                payload_hash=payload_hash,
                tenant_id=resolved.tenant_id,
                channel_id=resolved.channel_id,
            )
            logger.warning(
                "chatwoot webhook: unsupported bot channel kind=%s tenant=%s inbox=%s",
                resolved.channel_kind,
                resolved.tenant_id,
                inbox_id,
            )
            return Response(status_code=status.HTTP_200_OK)
        # Chatwoot owns the channel now; the customer's provider-side id
        # (WA wa_id / PSID / chat id) lives on conversation.contact_inbox.
        # Build the IncomingMessage on the REAL channel so pipeline.run replies
        # via Meta to the right person — NOT provider='chatwoot' (rejected),
        # NOT the Chatwoot contact id (undeliverable).
        recipient = extract_chatwoot_recipient(conversation, resolved.channel_kind)
        if recipient is None:
            await record_event(
                source="chatwoot",
                external_id=external_id,
                status="no_recipient",
                payload_hash=payload_hash,
                tenant_id=resolved.tenant_id,
                channel_id=resolved.channel_id,
            )
            logger.warning(
                "chatwoot webhook: unresolved recipient source_id "
                "tenant=%s inbox=%s channel=%s — skipping enqueue",
                resolved.tenant_id,
                inbox_id,
                resolved.channel_kind,
            )
            return Response(status_code=status.HTTP_200_OK)

        sender_obj = payload.get("sender") or {}
        sender_name = sender_obj.get("name") if isinstance(sender_obj, dict) else None
        content = payload.get("content")
        body = content if isinstance(content, str) else None
        attachments = _extract_chatwoot_attachments(payload)
        in_reply_to = _extract_chatwoot_reply_id(payload)
        # Chatwoot owns the conversation — thread its existing conversation +
        # account ids so the pipeline LINKS (never re-creates) the CW mirror.
        cw_conv_raw = conversation.get("id") if isinstance(conversation, dict) else None
        cw_account = payload.get("account")
        cw_acct_raw = cw_account.get("id") if isinstance(cw_account, dict) else None

        created_at_raw = payload.get("created_at")
        provider_sent_at: datetime | None = None
        if isinstance(created_at_raw, str):
            try:
                provider_sent_at = datetime.fromisoformat(
                    created_at_raw.replace("Z", "+00:00")
                )
            except ValueError:
                provider_sent_at = None

        message = IncomingMessage(
            tenant_id=resolved.tenant_id,
            channel_id=resolved.channel_id,
            provider=channel_kind_to_provider(resolved.channel_kind),
            provider_message_id=external_id,
            sender=SenderRef(
                channel_user_id=recipient,
                display_name=sender_name if isinstance(sender_name, str) else None,
            ),
            body=body,
            attachments=attachments,
            received_at=received_at,
            provider_sent_at=provider_sent_at,
            in_reply_to=in_reply_to,
            chatwoot_conversation_id=cw_conv_raw if isinstance(cw_conv_raw, int) else None,
            chatwoot_account_id=cw_acct_raw if isinstance(cw_acct_raw, int) else None,
        )
        if not await enqueue(message):
            # Queue full → message dropped. The idempotency claim is already
            # set, so returning 200 here would make the provider's retry
            # dedupe as replay_ignored = a customer message with no reply,
            # silently lost. Release the claim + 503 so the retry
            # RE-processes (recoverable). Mirrors the email-assist branch
            # above (lines ~712-727).
            await idem_release(redis, "chatwoot", external_id)
            logger.error(
                "chatwoot webhook: inbound queue full — released idempotency "
                "claim + 503 for provider retry tenant=%s channel=%s",
                resolved.tenant_id,
                resolved.channel_kind,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="inbound_backpressure",
            )
        await record_event(
            source="chatwoot",
            external_id=external_id,
            status=DECISION_TO_EVENT_STATUS[StatusDecision.PROCEED],  # type: ignore[arg-type]
            payload_hash=payload_hash,
            tenant_id=resolved.tenant_id,
            channel_id=resolved.channel_id,
        )
        return Response(status_code=status.HTTP_200_OK)

    except (asyncpg.PostgresError, redis_exc.RedisError) as exc:
        raise _backend_outage_exc("message_created", exc) from exc
