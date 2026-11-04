"""Email-assist — Chatwoot-native email channel, human-first bot-assist (M2).

PRD: .claude/prds/email-channel.prd.md. On every inbound customer email the bot
AUTO-DRAFTS a suggested reply and posts it as a Chatwoot PRIVATE NOTE for a human
agent to send — it never auto-sends to the customer (RD #5: auto-draft, not
auto-reply). Reuses the chat stack's RAG + LLM (embed → search_hybrid →
generate_response), the same quota meter as bot replies (RD #8), and the existing
``ChatwootClient.post_private_note`` (RD #10). Acceptance is tracked by comparing
the posted note to the agent's actual sent reply (RD #15).

This module holds the orchestration + pure helpers; the queue/worker
(``email_assist_queue`` / ``email_assist_worker``) drive it off the Chatwoot
webhook. Kept entirely separate from the customer-reply pipeline, which rejects
``provider='chatwoot'`` (``pipeline.py:157``).
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

import asyncpg
import redis.asyncio as redis_async

from polaris_api.db.pools import get_service_pool
from polaris_api.services import embed, llm, quota, rag, tenant_snapshot
from polaris_api.services._llm_prompts import (
    TenantPersona,
    coerce_persona_blocks,
    format_rag_context,
)
from polaris_api.services.chatwoot.client import ChatwootClient, ChatwootConfigError
from polaris_api.services.chatwoot.label_sync import sync_handling_label

logger = logging.getLogger(__name__)

# --- Cost guard (PRD RD #9) -------------------------------------------------
# Skip drafting on obvious non-customer mail so noisy inboxes don't burn LLM
# calls. These signals are PROVISIONAL pending the M0 webhook-payload capture
# (PRD Open Q "cost-guard header signals") — they are the conservative defaults.
# Tune the localparts / header keys HERE once M0 confirms what Chatwoot actually
# surfaces in the message_created payload.
_NON_CUSTOMER_LOCALPARTS: Final[tuple[str, ...]] = (
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces", "notifications",
)
# Header-ish keys (lowercased) that mark bulk / automated mail.
_BULK_HEADER_KEYS: Final[tuple[str, ...]] = (
    "list-unsubscribe", "precedence", "auto-submitted", "x-auto-response-suppress",
)

# --- Acceptance classification (PRD RD #15) ---------------------------------
# Similarity of the agent's sent reply vs. the posted draft. Thresholds are
# PROVISIONAL — tune in E2E once real drafts + replies exist.
_ACCEPTED_AT: Final = 0.85
_EDITED_AT: Final = 0.40

# Email drafts default to the 'medium' tier (Haiku, non-stream). Quality is
# shaped by the tenant's email_draft_prompt; bump to 'hard' (Sonnet) if needed.
_EMAIL_DRAFT_INTENT: Final = "medium"

# Truncation caps. Email bodies + agent replies are tenant-/sender-influenced,
# so cap before the LLM (token cost) and before the O(n²) SequenceMatcher
# (event-loop time, since record_agent_reply runs in the webhook request path).
_MAX_EMAIL_BODY_CHARS: Final = 16_000
_SIMILARITY_MAX_CHARS: Final = 4_000


@dataclass(slots=True)
class EmailAssistJob:
    """One inbound customer email to draft a reply for, derived from a Chatwoot
    ``message_created`` webhook. Carries only what the assist needs — Chatwoot
    owns the raw email."""

    tenant_id: UUID
    channel_id: UUID | None
    chatwoot_account_id: int
    chatwoot_conversation_id: int
    sender_email: str | None
    body: str
    signals: dict[str, Any] = field(default_factory=dict)


def build_job(
    payload: dict[str, Any], *, tenant_id: UUID, channel_id: UUID | None
) -> EmailAssistJob | None:
    """Construct an ``EmailAssistJob`` from a Chatwoot message_created payload.

    Returns ``None`` when the payload lacks the account/conversation ids the
    assist needs (the caller then no-ops + acks 200)."""
    account = payload.get("account")
    account_id = account.get("id") if isinstance(account, dict) else None
    conversation = payload.get("conversation")
    conversation_id = conversation.get("id") if isinstance(conversation, dict) else None
    if not isinstance(account_id, int) or not isinstance(conversation_id, int):
        return None

    content = payload.get("content")
    body = (content if isinstance(content, str) else "")[:_MAX_EMAIL_BODY_CHARS]
    sender = payload.get("sender") or {}
    sender_email = sender.get("email") if isinstance(sender, dict) else None

    # Header-ish signals for the cost guard. Chatwoot surfaces email metadata
    # under content_attributes.email; exact keys confirmed in M0. Best-effort
    # until then — an empty signals dict just means the sender check carries it.
    signals: dict[str, Any] = {}
    ca = payload.get("content_attributes")
    if isinstance(ca, dict):
        email_meta = ca.get("email")
        if isinstance(email_meta, dict):
            for k, v in email_meta.items():
                signals[str(k).lower()] = v

    return EmailAssistJob(
        tenant_id=tenant_id,
        channel_id=channel_id,
        chatwoot_account_id=account_id,
        chatwoot_conversation_id=conversation_id,
        sender_email=sender_email if isinstance(sender_email, str) else None,
        body=body,
        signals=signals,
    )


def should_skip_draft(job: EmailAssistJob) -> str | None:
    """Return a reason string when this email is obvious non-customer mail (so
    we skip the draft + the LLM cost), else ``None``. Pure — unit-testable
    without a live fork."""
    sender = (job.sender_email or "").strip().lower()
    if sender:
        local = sender.split("@", 1)[0]
        for tok in _NON_CUSTOMER_LOCALPARTS:
            # Exact, or a delimiter-bounded compound like "bounces-12345@" — NOT
            # a bare prefix, which would wrongly skip "bouncer@" (a real person).
            if local == tok or local.startswith(
                (f"{tok}-", f"{tok}.", f"{tok}_", f"{tok}+")
            ):
                return f"non_customer_sender:{tok}"
    signals = {str(k).lower(): v for k, v in (job.signals or {}).items()}
    for key in _BULK_HEADER_KEYS:
        if signals.get(key):
            return f"bulk_header:{key}"
    if not (job.body or "").strip():
        return "empty_body"
    return None


def classify_outcome(suggestion: str, agent_reply: str) -> tuple[str, float]:
    """Classify how much the agent reused the draft, by text similarity.

    Inputs are capped before the O(n²) SequenceMatcher so a long quoted email
    thread can't stall the event loop (record_agent_reply runs in the webhook
    request path)."""
    a = suggestion.strip()[:_SIMILARITY_MAX_CHARS]
    b = agent_reply.strip()[:_SIMILARITY_MAX_CHARS]
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= _ACCEPTED_AT:
        return "accepted", ratio
    if ratio >= _EDITED_AT:
        return "edited", ratio
    return "ignored", ratio


async def generate_and_post_draft(
    job: EmailAssistJob,
    *,
    pool: asyncpg.Pool,
    redis: redis_async.Redis,
    chatwoot: ChatwootClient,
) -> str:
    """Draft a reply for one inbound email and post it as a Chatwoot private
    note. Returns ``'posted'`` or ``'skipped:<reason>'``. LLM / Chatwoot errors
    propagate to the worker's per-job catch (the message row is Chatwoot's)."""
    skip = should_skip_draft(job)
    if skip is not None:
        logger.info(
            "email_assist: skip tenant=%s conv=%s reason=%s",
            job.tenant_id, job.chatwoot_conversation_id, skip,
        )
        return f"skipped:{skip}"

    query_embedding = await embed.embed_query(job.body, tenant_id=job.tenant_id)
    async with pool.acquire() as conn:
        # Enforce the same monthly cap as bot replies (RD #8) — a draft is an
        # LLM call too. Gate the expensive generate_response behind it so an
        # over-cap tenant's noisy inbox can't run up unbounded spend.
        quota_status = await quota.check_quota(conn, redis, tenant_id=job.tenant_id)
        if quota_status.over_cap:
            logger.info(
                "email_assist: skip tenant=%s conv=%s reason=quota_exceeded",
                job.tenant_id, job.chatwoot_conversation_id,
            )
            return "skipped:quota_exceeded"
        chunks = await rag.search_hybrid(
            conn=conn,
            tenant_id=job.tenant_id,
            query_text=job.body,
            query_embedding=query_embedding,
            redis=redis,
        )
        snapshot = await tenant_snapshot.load_tenant_snapshot(conn, job.tenant_id)

    persona = TenantPersona(
        assistant_name=snapshot.get("persona_assistant_name"),
        company_name=snapshot.get("persona_company_name"),
        system_prompt_override=snapshot.get("system_prompt"),
        persona_blocks=coerce_persona_blocks(snapshot.get("persona_blocks")),
    )
    response = await llm.generate_response(
        tenant_id=job.tenant_id,
        rag_context=format_rag_context(chunks),
        conversation_history=[],
        user_message=job.body,
        intent=_EMAIL_DRAFT_INTENT,
        chunks=chunks,
        persona=persona,
        channel="email",
        email_instructions=snapshot.get("email_draft_prompt"),
    )

    note_id = await chatwoot.post_private_note(
        account_id=job.chatwoot_account_id,
        conversation_id=job.chatwoot_conversation_id,
        content=response.content,
    )

    # Tag the conversation `human-handling` so it surfaces in the agents'
    # human-handling queue and stays human-owned — email is human-first: the bot
    # only drafts, never sends. Same label + convention as the pipeline handoff
    # path. Best-effort: a label failure must not lose the already-posted note.
    try:
        await sync_handling_label(
            account_id=job.chatwoot_account_id,
            conversation_id=job.chatwoot_conversation_id,
            owner="human",
            chatwoot=chatwoot,
        )
    except ChatwootConfigError:
        # Missing CHATWOOT_AGENT_API_TOKEN — label endpoints reject the bot
        # token, so this is a deploy-config error, not a transient blip. Log
        # ERROR so it stands out from outages instead of a per-job warning flood.
        logger.error(
            "email_assist: human-handling label skipped — CHATWOOT_AGENT_API_TOKEN "
            "unset (label endpoints need the user token); set it in .env",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "email_assist: human-handling label failed tenant=%s conv=%s",
            job.tenant_id, job.chatwoot_conversation_id,
        )

    # Route to a human: flip the conversation pending -> open so it leaves the
    # bot-owned state and surfaces in agents' Open/Mine views (a pending,
    # unassigned conversation is hidden from the default inbox). With Chatwoot
    # inbox auto-assignment enabled, an open + unassigned conversation is then
    # round-robin assigned to an available agent. Best-effort: a flip failure
    # must not lose the already-posted draft note.
    try:
        await chatwoot.flip_conversation_status(
            account_id=job.chatwoot_account_id,
            conversation_id=job.chatwoot_conversation_id,
            status="open",
        )
    except ChatwootConfigError:
        logger.error(
            "email_assist: status flip to open skipped — Chatwoot not configured",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "email_assist: status flip to open failed tenant=%s conv=%s",
            job.tenant_id, job.chatwoot_conversation_id,
        )

    # Meter the draft exactly like a bot reply (RD #8). Best-effort: a Redis
    # hiccup must not lose the already-posted note.
    try:
        await quota.increment_counter(redis, tenant_id=job.tenant_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "email_assist: quota increment failed tenant=%s — nightly flush reconciles",
            job.tenant_id,
        )

    await _record_draft(
        pool, job=job, suggestion_text=response.content, note_message_id=note_id
    )
    logger.info(
        "email_assist: drafted tenant=%s conv=%s note_id=%s",
        job.tenant_id, job.chatwoot_conversation_id, note_id,
    )
    return "posted"


async def _record_draft(
    pool: asyncpg.Pool,
    *,
    job: EmailAssistJob,
    suggestion_text: str,
    note_message_id: int,
) -> None:
    """Persist the posted draft for later acceptance matching (RD #15)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_drafts (
              tenant_id, channel_id, chatwoot_account_id,
              chatwoot_conversation_id, chatwoot_note_message_id, suggestion_text
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            job.tenant_id,
            job.channel_id,
            job.chatwoot_account_id,
            job.chatwoot_conversation_id,
            note_message_id,
            suggestion_text,
        )


async def record_agent_reply(
    *,
    tenant_id: UUID,
    chatwoot_conversation_id: int,
    agent_reply_text: str,
) -> None:
    """Match an agent's sent reply to the latest pending draft for the
    conversation and record accepted / edited / ignored (RD #15). No-op when
    there's no pending draft (e.g. the agent opened a fresh thread).

    Acquires the service pool internally so the Chatwoot webhook router (the
    caller) never imports a BYPASSRLS pool — keeping the router's
    "DB work goes through a service" boundary intact."""
    if not agent_reply_text.strip():
        return
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, suggestion_text
              FROM email_drafts
             WHERE tenant_id = $1
               AND chatwoot_conversation_id = $2
               AND outcome = 'pending'
             ORDER BY posted_at DESC
             LIMIT 1
            """,
            tenant_id,
            chatwoot_conversation_id,
        )
        if row is None:
            return
        outcome, ratio = classify_outcome(row["suggestion_text"], agent_reply_text)
        await conn.execute(
            """
            UPDATE email_drafts
               SET agent_reply_text = $2, similarity = $3,
                   outcome = $4, resolved_at = now()
             WHERE id = $1
               AND tenant_id = $5
            """,
            row["id"],
            agent_reply_text,
            ratio,
            outcome,
            tenant_id,
        )
    logger.info(
        "email_assist: acceptance tenant=%s conv=%s outcome=%s similarity=%.2f",
        tenant_id, chatwoot_conversation_id, outcome, ratio,
    )
