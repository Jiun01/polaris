"""Inbound-pipeline orchestrator — Phase 4 PR-4.

Single function ``run(incoming, deps)`` walks one ``IncomingMessage``
through the full LLM pipeline:

  1. Acquire connection from anon pool; set ``app.current_tenant_id``
     GUC inside a transaction so RLS is enforced for every read +
     write that follows.
  2. Ensure a ``conversations`` row exists for (tenant, channel,
     channel_user_id) — INSERT-then-SELECT race-safe pattern.
  3. Save the inbound user message to ``messages``.
  4. Classify intent via Haiku (services/llm.classify_intent).
  5. If intent='handoff': emit handoff template + return early
     (deep-dive §9.7 — never silent on handoff).
  6. Embed query (services/embed.embed_query — PR-4 stub).
  7. Hybrid RAG search with tier-gated Cohere rerank
     (services/rag.search_hybrid).
  8. Load conversation window (services/conversation.load_history),
     excluding the just-saved user turn (it's the current message).
  9. Generate response (services/llm.generate_response).
 10. Persist assistant message with full token telemetry.
 11. Bump ``conversations.last_message_at``.
 12. Dispatch outbound via services/reply_dispatch (OUTSIDE the
     transaction — sending is the last side-effect; a Meta 5xx
     after a committed reply is recoverable via webhook retry, but
     a committed-but-uncommitted-reply would be a phantom turn).
 13. Update messages.provider_message_id with the returned wamid
     (best-effort second tx).

Architectural invariants for this file (re-verify on every change):
  - have ZERO ``while`` loops
  - have ZERO ``for ... in retry_attempts`` patterns
  - use at most ONE ``tenacity.AsyncRetrying`` and only at the LLM
    boundary (PR-4 doesn't add one — Anthropic SDK's own retry
    semantics are enough; revisit if production telemetry shows
    burst-time pain)

OTel spans nest under one parent ``pipeline.run`` per inbound — the
Phase 9 per-intent-tier dashboard reads the parent's ``intent_tier``
+ ``model_used`` attributes. Children (``classify``, ``rag``,
``history``, ``generate``, ``dispatch``) inherit context but don't
carry the high-cardinality labels themselves.

PR-4 ships WITHOUT OTel SDK wiring — the spans are placeholder
log lines so the structure exists without forcing the OTel
dependency stack. Phase 9 wires OpenTelemetry properly.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

import asyncpg
import httpx
import redis.asyncio as redis_async
from polaris_shared_types import (
    ConversationWindow,
    IncomingMessage,
    IntentTier,
    LanguageDetected,
    PipelineDecision,
    Provider,
    ResponseEnvelope,
    UsageMetrics,
)

from polaris_api import metrics
from polaris_api.config import get_settings
from polaris_api.services import (
    conversation as conversation_service,
)
from polaris_api.services import (
    embed,
    escalation,
    llm,
    quota,
    rag,
    reply_dispatch,
    stop_keyword,
)
from polaris_api.services import tenant_snapshot as tenant_snapshot_svc
from polaris_api.services._llm_models import LLMHardFailure
from polaris_api.services._llm_prompts import (
    TenantPersona,
    coerce_persona_blocks,
    format_products_context,
    format_promotions_context,
    format_rag_context,
)
from polaris_api.services.agent import pipeline_integration as agent_integration
from polaris_api.services.audience.members import upsert_audience_member
from polaris_api.services.audience.opt_out import mark_opted_out
from polaris_api.services.booking import conversational as booking_conversational
from polaris_api.services.booking.slot_picker_state import BookingPickerState
from polaris_api.services.followups import scheduler as followup_scheduler
from polaris_api.services.knowledge_gaps import capture as knowledge_gap_capture
from polaris_api.services.meta_messaging import (
    MetaMessagingError,
    get_instagram_login_client,
    get_meta_messaging_client,
)
from polaris_api.services.meta_whatsapp import (
    MetaWhatsAppClient,
    MetaWhatsAppError,
)

# The 3 modules below were extracted from this file in Batch E (task E1; see
# PRP plan audit-batch-e-structural-splits): voice-note handling,
# quota-exhausted escalation, and Chatwoot reply-mirroring / product-image
# minting. Imported here at module top level (safe — none of them needs
# pipeline.py at IMPORT time, only at CALL time); each extracted module calls
# back into THIS module's persistence/dispatch helpers via a late
# (function-body) import of `pipeline` itself, mirroring this file's own
# existing late-import discipline for chatwoot.*/secrets/demo_*/
# products.storage (see e.g. _escalate_quota_exhausted's late `chatwoot.handoff`
# import before this split, or _mirror_reply_to_cw's late `chatwoot.client`
# import — both preserved unchanged inside the moved functions).
from polaris_api.services.pipeline_cw_mirror import (
    _mint_product_image_urls,
    _mirror_reply_to_cw,
)
from polaris_api.services.pipeline_quota_escalation import _escalate_quota_exhausted
from polaris_api.services.pipeline_voice import (
    _find_audio_attachment,
    _handle_inbound_voice_note,
)
from polaris_api.services.products import search as products_search
from polaris_api.services.promotions import lookup as promotions_lookup
from polaris_api.services.reply_dispatch import ConversationRef, DispatchSkipped
from polaris_api.services.telegram_client import TelegramError, get_telegram_client

logger = logging.getLogger(__name__)

_PROVIDER_TO_CHANNEL: Final[dict[Provider, str]] = {
    "whatsapp": "whatsapp",
    "messenger": "messenger",
    "chatwoot": "chatwoot",  # explicitly mapped so the rejection error has the right tag
    "telegram": "telegram",  # off-Meta channels M1
    "instagram_login": "instagram_login",  # graph.instagram.com (migration 104)
}


_HANDOFF_REPLY_TEXT: Final[str] = (
    "I'm passing this conversation to a human agent, they'll respond to you shortly."
)


_HANDOFF_LATENCY_PLACEHOLDER_MS: Final[int] = 0


_PRODUCT_RELEVANCE_MAX_DISTANCE: Final[float] = 0.30


def product_passes_relevance_gate(distance: float | None) -> bool:
    """Decide whether one catalogue hit clears the product-injection gate.

    Pure predicate extracted from the default-path filter so it is unit-testable
    in isolation. Keep BM25-only hits (``distance is None`` ⇒ matched lexically,
    no vector score) and vector hits strictly nearer than the 0.30 cosine gate;
    drop everything at or beyond it (off-topic products)."""
    return distance is None or distance < _PRODUCT_RELEVANCE_MAX_DISTANCE


_REFERENTIAL_WORDS: Final[frozenset[str]] = frozenset(
    {
        "it",
        "its",
        "that",
        "this",
        "these",
        "those",
        "one",
        "ones",
        "them",
        "they",
        "there",
        "then",
        "another",
        "same",
        "cheaper",
        "bigger",
        "smaller",
        "better",
        "instead",
        "itu",
        "ni",
        "tu",
        "lagi",  # incl. Malay deictics
    }
)


_REFERENTIAL_PHRASES: Final[tuple[str, ...]] = (
    "what about",
    "how about",
    "and the",
    "what else",
    "which one",
    "yang mana",
    "macam mana",
)


def _looks_referential(text: str) -> bool:
    """Heuristic: does this message likely need prior turns to search correctly?

    Bounds the cost of follow-up condensation — only likely-referential messages
    pay for the extra Haiku call. Triggers on a referential/comparative cue or a
    very short message (<= 3 words, or a single CJK run with no spaces). Errs
    toward NOT condensing so long self-contained questions skip it.
    """
    t = text.strip().lower()
    if not t:
        return False
    if any(p in t for p in _REFERENTIAL_PHRASES):
        return True
    if set(re.findall(r"[a-z]+", t)) & _REFERENTIAL_WORDS:
        return True
    return len(t.split()) <= 3


_VOICE_NOTE_PREFIX: Final[str] = "Voice Note: "


@dataclass(slots=True)
class PipelineDeps:
    """Injected dependencies — keeps ``run()`` testable.

    The orchestrator never reaches for module-level singletons; all
    side-effecting objects come through here. Tests pass mocks via
    PipelineDeps construction.
    """

    anon_pool: asyncpg.Pool
    redis: redis_async.Redis
    meta_client: MetaWhatsAppClient


_DeferredCwAction = Callable[[], Awaitable[None]]


async def _flush_deferred_cw(actions: list[_DeferredCwAction]) -> None:
    """Run best-effort Chatwoot side-effects collected during the pipeline
    transaction, AFTER the DB connection is released — so a slow Chatwoot HTTP
    call never holds an asyncpg pool slot open across the transaction. Each
    action self-handles its own errors; this loop is a backstop."""
    for action in actions:
        try:
            await action()
        except Exception:  # noqa: BLE001
            logger.warning("pipeline: deferred Chatwoot side-effect failed", exc_info=True)


async def _apply_handling_label_deferred(
    *,
    account_id: int,
    conversation_id: int,
    owner: Literal["bot", "human"],
    tenant_id: UUID,
) -> None:
    """Sync the bot-handling / human-handling ownership label (deferred form).

    Best-effort: a Chatwoot outage must never affect the customer-facing reply.
    Imports are local so the chatwoot client/label module stays optional for
    callers that never link a CW conversation.
    """
    try:
        from polaris_api.services.chatwoot.client import (
            get_chatwoot_client as _get_cw_client,
        )
        from polaris_api.services.chatwoot.label_sync import sync_handling_label

        await sync_handling_label(
            account_id=account_id,
            conversation_id=conversation_id,
            owner=owner,
            chatwoot=_get_cw_client(),
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pipeline: CW %s-handling label sync failed tenant=%s cw_conv=%s",
            owner,
            tenant_id,
            conversation_id,
            exc_info=True,
        )


async def _post_voice_note_deferred(
    *, account_id: int, conversation_id: int, content: str, tenant_id: UUID
) -> None:
    """Post the voice-note transcript as a Chatwoot private note (deferred form).

    Best-effort; failure never blocks the reply. No DB dependency — safe to run
    after the pipeline transaction commits.
    """
    try:
        from polaris_api.services.chatwoot.client import (
            get_chatwoot_client as _get_cw_client,
        )

        await _get_cw_client().post_private_note(
            account_id=account_id,
            conversation_id=conversation_id,
            content=content,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pipeline: voice-note private note failed tenant=%s cw_conv=%s",
            tenant_id,
            conversation_id,
            exc_info=True,
        )


async def run(
    *,
    incoming: IncomingMessage,
    deps: PipelineDeps,
    demo: bool = False,
) -> PipelineDecision:
    """Execute the full inbound pipeline for one message.

    ``demo`` (ADR-066): when True this is a BRAND-demo turn (content scoped to a
    hidden demo tenant). Demo turns skip audience enrollment + quota and do not
    consume the tenant's bot-reply counter. Reply dispatch resolves credentials
    against the channel's OWNER tenant (the gateway), so a brand-scoped turn
    still sends from the operator's real channel — see ``_dispatch_reply``.

    Thin wrapper over ``_run_inner``. The conn-independent best-effort Chatwoot
    side-effects — the bot/human-handling ownership labels, the voice-note
    transcript note, and (as of the A4 silent-failure audit fix, 2026-07-02)
    the handoff recovery note's HTTP-post-and-marker-clear — are collected into
    ``deferred_cw`` during the transaction and flushed here in a ``finally`` —
    AFTER ``_run_inner``'s transaction has committed and its asyncpg
    connection is released — so those slow Chatwoot HTTP calls no longer pin a
    pool connection across the transaction (they previously ran inline inside
    the txn). The flush runs on every exit path, including the
    booking/agent/flow/quota short-circuit returns.

    Still inline (intentionally): the quota-escalation CW flip/label (rare
    over-cap path). Deferring it needs it to self-acquire a connection — out
    of scope here; tracked as a follow-up.
    """
    deferred_cw: list[_DeferredCwAction] = []
    try:
        return await _run_inner(incoming=incoming, deps=deps, deferred_cw=deferred_cw, demo=demo)
    finally:
        await _flush_deferred_cw(deferred_cw)


async def _run_inner(
    *,
    incoming: IncomingMessage,
    deps: PipelineDeps,
    deferred_cw: list[_DeferredCwAction],
    demo: bool = False,
) -> PipelineDecision:
    """Execute the full inbound pipeline for one message.

    Caller owns:
      - tenant_id resolution + status gate (Phase 2 dispatcher does this)
      - idempotency dedup (Phase 2 ``services/idempotency.py``)

    Raises:
      - ValueError if provider='chatwoot' (wrong pipeline)
      - LLMHardFailure if both Anthropic AND OpenRouter fail
        (caller's apology-reply path)
      - asyncpg.PostgresError on RLS / FK violations (means a bug
        upstream — pipeline.run does NOT swallow)
    """
    t0 = time.monotonic()

    if incoming.provider == "chatwoot":
        # Chatwoot inbound = agent reply going OUT to customer; Phase 7
        # owns that flow. Hard-fail here so a future webhook routing
        # bug doesn't silently miss-process operator messages as
        # customer messages.
        raise ValueError(
            "pipeline.run rejects provider='chatwoot' — that's Phase 7's "
            "handoff path, not the bot pipeline."
        )

    channel = _PROVIDER_TO_CHANNEL[incoming.provider]
    user_text = incoming.body or ""

    # Inbound voice notes (voice-notes M1). If the message carries an audio
    # attachment, attempt transcription (Pro+ only) and substitute the
    # transcript as ``user_text`` so the rest of the pipeline runs unchanged
    # and replies in the spoken language. Gated-out (Starter) and failed
    # cases reply gracefully + return early. Runs BEFORE the media-only→
    # handoff branch so a voice note is no longer treated as unhandled media.
    is_voice_note = False
    voice_note_text: str | None = None
    audio_attachment = _find_audio_attachment(incoming)
    if audio_attachment is not None and get_settings().transcription_enabled:
        voice = await _handle_inbound_voice_note(
            incoming=incoming,
            channel=channel,
            deps=deps,
            t0=t0,
            attachment=audio_attachment,
        )
        if voice.decision is not None:
            return voice.decision
        if voice.transcript is not None:
            user_text = voice.transcript
            is_voice_note = True
            voice_note_text = voice.transcript

    if not user_text.strip():
        # Empty inbound (media-only message with no caption, or a voice note
        # with transcription disabled). Phase 4 doesn't process other media —
        # emit a polite ask-for-text reply via the handoff path so the user
        # still gets a response.
        logger.info(
            "pipeline.run: empty body (media-only) tenant=%s — emitting handoff",
            incoming.tenant_id,
        )
        return await _emit_handoff_for_empty_body(
            incoming=incoming,
            channel=channel,
            deps=deps,
            t0=t0,
        )

    # Populated inside the transaction if CW conversation is created/found.
    # Used after dispatch to mirror the bot reply into CW (best-effort).
    _cw_account_id: int | None = None
    _cw_conv_id: int | None = None

    async with deps.anon_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(incoming.tenant_id),
            )

            # conversation_branch_id (v2.1 stamping) scopes both the agent
            # adapters and the classic product/promo lookups to this branch.
            conversation_id, conversation_branch_id = await _ensure_conversation(
                conn=conn,
                tenant_id=incoming.tenant_id,
                channel=channel,
                channel_user_id=incoming.sender.channel_user_id,
                channel_id=incoming.channel_id,
            )

            # Follow-up: the customer is active again, so cancel any pending
            # AI re-engagement nudge for this conversation. A qualifying bot
            # reply below reschedules a fresh one.
            await followup_scheduler.cancel_pending_followup(
                conn, tenant_id=incoming.tenant_id, conversation_id=conversation_id
            )

            # Chatwoot-native: link the EXISTING Chatwoot conversation (its ids
            # ride in on the agent-bot payload) onto the Polaris row so the
            # bot-handling label / handoff / reply-mirror target it. Chatwoot
            # owns conversation creation now — we never create (that produced a
            # duplicate CW conversation). Idempotent UPDATE inside the txn.
            _cw_account_id, _cw_conv_id = await _link_cw_conversation(
                conn=conn,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
                incoming=incoming,
            )

            # Bot owns every pipeline-processed conversation — the agent-bot
            # gate only admits status==pending. Mirror that into CW as the
            # bot-handling label so agents can filter bot activity; the
            # handoff/escalation path swaps it to human-handling. Best-effort,
            # read-modify-write (preserves other labels), GET-only once synced.
            if _cw_account_id and _cw_conv_id:
                # Deferred: synced AFTER the txn commits + the connection is
                # released (run()'s finally) so the Chatwoot HTTP call never
                # holds an asyncpg pool slot across the transaction.
                deferred_cw.append(
                    functools.partial(
                        _apply_handling_label_deferred,
                        account_id=_cw_account_id,
                        conversation_id=_cw_conv_id,
                        owner="bot",
                        tenant_id=incoming.tenant_id,
                    )
                )

            # Voice-note transcript → Chatwoot private note so the agent can
            # verify what was said (best-effort; failure never blocks reply).
            # Conditional on a resolvable CW conversation — Telegram / unlinked
            # Meta inbound simply skip it.
            if is_voice_note and voice_note_text and _cw_account_id and _cw_conv_id:
                # Deferred (run()'s finally): no conn dependency, so it runs
                # after commit + connection release like the label syncs.
                deferred_cw.append(
                    functools.partial(
                        _post_voice_note_deferred,
                        account_id=_cw_account_id,
                        conversation_id=_cw_conv_id,
                        content=f"{_VOICE_NOTE_PREFIX}{voice_note_text}",
                        tenant_id=incoming.tenant_id,
                    )
                )

            # Phase 7 PR-3: Q-PHASE7-04 recovery note. Split per the
            # deferred_cw pattern (see module docstring in
            # services/chatwoot/recovery.py): the marker CHECK is cheap DB-only
            # and stays inline; the HTTP POST + marker CLEAR are deferred so a
            # slow/unreachable Chatwoot never holds this transaction's pooled
            # connection across the round trip.
            try:
                from polaris_api.services.chatwoot.client import (
                    get_chatwoot_client,
                )
                from polaris_api.services.chatwoot.recovery import (
                    check_pending_recovery,
                    post_recovery_note_deferred,
                )

                pending = await check_pending_recovery(
                    conn=conn,
                    tenant_id=incoming.tenant_id,
                    conversation_id=conversation_id,
                )
                if pending is not None:
                    deferred_cw.append(
                        functools.partial(
                            post_recovery_note_deferred,
                            pending=pending,
                            tenant_id=incoming.tenant_id,
                            conversation_id=conversation_id,
                            chatwoot=get_chatwoot_client(),
                            pool=deps.anon_pool,
                        )
                    )
            except Exception:  # noqa: BLE001 — never block inbound on recovery
                logger.exception(
                    "pipeline: handoff recovery hook crashed tenant=%s conv=%s",
                    incoming.tenant_id,
                    conversation_id,
                )

            user_msg_id = await conversation_service.save_message(
                conn=conn,
                redis=deps.redis,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
                role="user",
                content=user_text,
            )

            # Step 1.4 — Upsert the audience_members row for this contact.
            # A genuinely new customer's first inbound otherwise never
            # lands in the audience screen (rows came only from CSV import
            # / demo / api_sync before this). Non-fatal: a failed upsert
            # must never block the bot reply. Runs before the STOP check
            # so a STOP-first contact still gets a row that mark_opted_out
            # can flip (mark_opted_out only UPDATEs — it won't create one).
            # ADR-066: brand-demo turns skip audience enrollment — demo testers
            # are not the operator's broadcast audience.
            if not demo:
                try:
                    await upsert_audience_member(
                        conn,
                        tenant_id=incoming.tenant_id,
                        channel=channel,
                        channel_user_id=incoming.sender.channel_user_id,
                        channel_user_name=incoming.sender.display_name,
                    )
                except Exception:  # noqa: BLE001 — audience upsert is best-effort
                    logger.warning(
                        "pipeline: audience_members upsert failed tenant=%s "
                        "channel=%s — inbound still processed",
                        incoming.tenant_id,
                        channel,
                    )

            # Step 1.5 — STOP keyword check (Phase 10 PR-1).
            # Must run AFTER save_message (so the STOP text is in history)
            # and BEFORE the flow runtime / intent classify.  On match:
            # mark the contact opted out and return early — no reply sent.
            stop_match = stop_keyword.check(user_text)
            if stop_match.matched:
                logger.info(
                    "pipeline.stop_keyword: matched keyword=%r tenant=%s channel=%s user=%s",
                    stop_match.keyword,
                    incoming.tenant_id,
                    channel,
                    incoming.sender.channel_user_id,
                )
                try:
                    await mark_opted_out(
                        conn,
                        tenant_id=incoming.tenant_id,
                        channel=channel,
                        channel_user_id=incoming.sender.channel_user_id,
                        reason="stop_keyword",
                        opted_out_at_channel=channel,
                    )
                    # Persist matched keyword for audit trail (migration 057).
                    await conn.execute(
                        """
                        UPDATE audience_members
                           SET stop_keyword_matched = $1
                         WHERE tenant_id       = $2
                           AND channel         = $3
                           AND channel_user_id = $4
                        """,
                        stop_match.keyword,
                        incoming.tenant_id,
                        channel,
                        incoming.sender.channel_user_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "pipeline.stop_keyword: mark_opted_out failed "
                        "tenant=%s — user message still persisted",
                        incoming.tenant_id,
                    )
                return PipelineDecision(
                    conversation_id=conversation_id,
                    tenant_id=incoming.tenant_id,
                    user_message_id=user_msg_id,
                    stop_blocked=True,
                )

            # Step 1.55 — Human-handoff suppression (QA finding M1).
            # If this conversation is already handed to a human
            # (``handoff_at`` set, not yet cleared by a reverse-handoff),
            # the bot must stay SILENT: the customer's message is persisted
            # above and already visible to the agent in Chatwoot (native
            # inbound owns the customer turn), so generating a reply here
            # would talk over the human. The FIRST handoff still fires
            # normally — the classifier-handoff branch below sets
            # ``handoff_at`` only after this gate, so just SUBSEQUENT
            # inbounds are suppressed. ``reverse_handoff`` clears
            # ``handoff_at`` when the agent hands back → bot re-engages.
            handoff_at = await conn.fetchval(
                "SELECT handoff_at FROM conversations WHERE id = $1",
                conversation_id,
            )
            # ``handoff_at`` is a timestamptz → asyncpg yields ``datetime`` when
            # set, ``None`` otherwise. Check the type (not just truthiness) so the
            # gate is precise about the expected shape.
            if isinstance(handoff_at, datetime):
                logger.info(
                    "pipeline.handoff_suppressed: conv=%s handed off at %s "
                    "— bot stays silent (reverse-handoff re-engages)",
                    conversation_id,
                    handoff_at,
                )
                return PipelineDecision(
                    conversation_id=conversation_id,
                    tenant_id=incoming.tenant_id,
                    user_message_id=user_msg_id,
                    handoff_suppressed=True,
                )

            # Step 1.6 — Quota check (Phase 10 PR-1).
            # Read period cap from tenant_quotas + current Redis counter.
            # If the tenant is at/over cap, return early without generating
            # a reply (no LLM cost, no dispatch).
            quota_status = await quota.check_quota(
                conn,
                deps.redis,
                tenant_id=incoming.tenant_id,
            )
            if quota_status.over_cap and not demo:  # ADR-066: demo never quota-blocked
                logger.warning(
                    "pipeline.quota_exceeded: tenant=%s current=%d cap=%d",
                    incoming.tenant_id,
                    quota_status.current_count,
                    quota_status.period_cap,
                )
                # Step 5 (Chatwoot-native): graceful escalation rather than a
                # silent drop. Send the customer a holding message and hand the
                # Chatwoot conversation to a person (status=open + human-handling
                # label) so native auto-assignment routes it to an agent. The
                # bot quota is spent, so this holding message is NOT counted as a
                # bot reply. Best-effort — it never fails the inbound. The flip
                # to open also stops re-escalation: the next inbound is gated out
                # at the consumer (status != pending).
                await _escalate_quota_exhausted(
                    conn=conn,
                    deps=deps,
                    incoming=incoming,
                    channel=channel,
                    cw_account_id=_cw_account_id,
                    cw_conv_id=_cw_conv_id,
                )
                return PipelineDecision(
                    conversation_id=conversation_id,
                    tenant_id=incoming.tenant_id,
                    user_message_id=user_msg_id,
                    quota_exceeded=True,
                )
            if quota_status.at_soft_cap and not demo:
                logger.warning(
                    "pipeline.quota_soft_cap: tenant=%s current=%d cap=%d (≥80%%)",
                    incoming.tenant_id,
                    quota_status.current_count,
                    quota_status.period_cap,
                )

            # audit Batch B (B1): fetch the tenant config snapshot ONCE here —
            # previously the agent branch, the classic branch, and the
            # knowledge-gap capture gate each issued their own
            # `SELECT ... FROM tenants` (2-3x per inbound message). Every
            # downstream consumer below now reads from this one dict instead
            # of re-querying.
            tenant_snapshot = await tenant_snapshot_svc.load_tenant_snapshot(
                conn,
                incoming.tenant_id,
            )

            # Knowledge Gap Detection (M1): resolve the Ultra-tier capture gate
            # ONCE — the three capture sites below (handoff / Phase 4 retrieval /
            # flow) are mutually exclusive, so one read suffices. Short-circuits
            # the tier read entirely when the master switch is off. A non-Ultra
            # tenant (or disabled) logs nothing.
            _kg_settings = get_settings()
            capture_ok = _kg_settings.knowledge_gap_capture_enabled and (
                knowledge_gap_capture.plan_tier_in_capture_tier(
                    tenant_snapshot.get("plan_tier"), _kg_settings.knowledge_gap_tiers
                )
            )

            # Step 1.7 — Conversational booking commands (v2.1).
            # A customer replying CANCEL / RESCHEDULE / CONFIRM (or a
            # free-text variant) to a booking reminder is handled here,
            # deterministically, BEFORE the flow runtime — so it works
            # without the tenant authoring a flow. Returns None (fall
            # through) unless the message is a booking command AND the
            # customer owns a confirmed upcoming booking, so normal
            # traffic is unaffected.
            booking_decision = await _try_booking_command(
                conn=conn,
                deps=deps,
                incoming=incoming,
                conversation_id=conversation_id,
                user_msg_id=user_msg_id,
                t0=t0,
                channel=channel,
                cw_account_id=_cw_account_id,
                cw_conv_id=_cw_conv_id,
            )
            if booking_decision is not None:
                return booking_decision

            # ADR-063: the tool-calling agent takes precedence over the classic
            # classify→rag→generate path below when the tenant has it enabled
            # (default off → None → fall through, so existing tenants are
            # unchanged). The agent result drives persistence + dispatch via
            # the same _persist_assistant_message + _dispatch_reply helpers
            # used by the classic path so the outbound shape is identical
            # from the customer's POV. (audit Batch D retired the Phase 5
            # flow_runtime branch that used to sit between this and the
            # classify call below — it was dead code on every tenant that
            # mattered per ADR-063.)
            agent_decision = await _try_agent_runtime(
                conn=conn,
                deps=deps,
                incoming=incoming,
                conversation_id=conversation_id,
                user_msg_id=user_msg_id,
                t0=t0,
                channel=channel,
                cw_account_id=_cw_account_id,
                cw_conv_id=_cw_conv_id,
                snapshot=tenant_snapshot,
                tenant_branch_id=conversation_branch_id,
            )
            if agent_decision is not None:
                # Agent path returns early — count its reply against quota
                # too (same contract as the classic path below).
                await _increment_bot_reply_quota(
                    deps,
                    tenant_id=incoming.tenant_id,
                    provider_message_id=agent_decision.provider_message_id,
                    demo=demo,
                )
                return agent_decision

            # audit Batch B (B4): referential messages ("what about the KL
            # one?") pay for history + a query-condensation Haiku call anyway
            # (see the else-branch below), so for THOSE messages only, hoist
            # the history load here and fold classification + condensation
            # into ONE Haiku call via classify_and_condense — replacing what
            # would otherwise be classify_intent followed by a separate
            # condense_followup_query call. Non-referential messages keep the
            # original single classify_intent call (no history load, no
            # change in call count). ``window_pre`` is reused below so the
            # else-branch's history load doesn't re-fetch (Redis-cached
            # either way, but the round-trip is skipped entirely).
            window_pre: ConversationWindow | None = None
            search_query_pre: str | None = None
            intent: IntentTier
            if _looks_referential(user_text):
                window_pre = await conversation_service.load_history(
                    conn=conn,
                    redis=deps.redis,
                    tenant_id=incoming.tenant_id,
                    conversation_id=conversation_id,
                )
                history_messages_pre = window_pre.messages[:-1] if window_pre.messages else []
                history_dicts_pre = [
                    {"role": m.role, "content": m.content} for m in history_messages_pre
                ]
                if history_dicts_pre:
                    intent, search_query_pre = await llm.classify_and_condense(
                        history_dicts_pre, user_text, tenant_id=incoming.tenant_id
                    )
                else:
                    intent = await llm.classify_intent(user_text, tenant_id=incoming.tenant_id)
            else:
                intent = await llm.classify_intent(user_text, tenant_id=incoming.tenant_id)
            logger.info(
                "pipeline.classify tenant=%s conv=%s intent=%s",
                incoming.tenant_id,
                conversation_id,
                intent,
            )

            # Opt-in human escalation (report don't-know handling): load the
            # conversation's escalation state. If the bot offered a human last
            # turn and is awaiting consent, interpret THIS message as the answer.
            # A clear "yes" routes into the handoff branch below (reusing its
            # CW label-flip + handoff_at suppression). Anything else clears the
            # offer and we answer normally.
            esc_state = await _load_escalation_state(deps.redis, conversation_id)
            consent_escalate = False
            if esc_state.awaiting_consent and intent != "handoff":
                if escalation.interpret_consent(user_text) == "yes":
                    consent_escalate = True
                else:
                    esc_state = escalation.EscalationState()
                    await _save_escalation_state(deps.redis, conversation_id, esc_state)

            if intent == "handoff" or consent_escalate:
                # Handoff short-circuit: never silent (§9.7).
                assistant_msg_id = await _persist_handoff_message(
                    conn=conn,
                    redis=deps.redis,
                    tenant_id=incoming.tenant_id,
                    conversation_id=conversation_id,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )
                await _bump_last_message_at(conn, conversation_id, incoming.tenant_id)
                # Record the handoff on the conversation so SUBSEQUENT inbounds
                # are suppressed by the Step 1.55 gate above (QA finding M1).
                # The classifier handoff previously only applied the CW
                # human-handling label — with no DB state, the next message was
                # processed normally and the bot talked over the human.
                # Idempotent (only set on the first handoff); ``reverse_handoff``
                # clears ``handoff_at`` when the agent hands back, re-engaging
                # the bot.
                await conn.execute(
                    "UPDATE conversations SET handoff_at = now() "
                    "WHERE id = $1 AND handoff_at IS NULL",
                    conversation_id,
                )
                if consent_escalate:
                    # Customer accepted the bot's offer to connect: send the
                    # language-matched acceptance + clear the escalation state.
                    _lang = llm._detect_language(user_text)
                    handoff_response = _build_handoff_response(
                        text=escalation.accept_text(_lang),
                        language=_lang,
                    )
                    await _save_escalation_state(
                        deps.redis, conversation_id, escalation.EscalationState()
                    )
                else:
                    handoff_response = _build_handoff_response()
                # Falls through to dispatch below (after transaction commit).
                response: ResponseEnvelope = handoff_response
                handoff_emitted = True
                rag_chunks_for_decision: list[Any] = []
                # Knowledge Gap Detection (M1): a real question routed to a human
                # is a miss — the bot had no confident answer. This is the
                # classifier-handoff path (an actual question); the media-only /
                # empty-body handoff path is excluded as unsupported-media noise,
                # not a knowledge gap. Fail-soft; Ultra-gated via capture_ok.
                if capture_ok:
                    await knowledge_gap_capture.record_miss(
                        conn,
                        tenant_id=incoming.tenant_id,
                        conversation_id=conversation_id,
                        message_id=user_msg_id,
                        customer_question=user_text,
                        signal_type="handoff",
                        retrieval_top_score=None,
                        channel=channel,
                        detected_language=None,
                    )
                # Phase 12: mirror handoff intent to CW as a label so agents
                # see the conversation in the human-handling queue.  Best-effort
                # — a CW outage must not block the customer-facing handoff reply.
                if _cw_account_id and _cw_conv_id:
                    # Deferred (run()'s finally): runs after the bot-handling
                    # append above, so the read-modify-write ends on
                    # human-handling — same net label as the prior inline pair,
                    # without holding the pool slot during the Chatwoot call.
                    deferred_cw.append(
                        functools.partial(
                            _apply_handling_label_deferred,
                            account_id=_cw_account_id,
                            conversation_id=_cw_conv_id,
                            owner="human",
                            tenant_id=incoming.tenant_id,
                        )
                    )
            else:
                # 6. Conversation window FIRST — needed both for follow-up query
                # condensation (F6) and for the LLM generation below. Exclude the
                # just-saved user turn (it's the current message, threaded
                # separately into generate_response via ``user_message=``).
                # audit Batch B (B4): reuse the hoisted window_pre when the
                # classify step above already loaded it (referential message)
                # instead of loading it a second time — Redis-cached either
                # way, but this skips the round-trip entirely.
                if window_pre is not None:
                    window = window_pre
                else:
                    window = await conversation_service.load_history(
                        conn=conn,
                        redis=deps.redis,
                        tenant_id=incoming.tenant_id,
                        conversation_id=conversation_id,
                    )
                history_messages = window.messages[:-1] if window.messages else []
                history_dicts = [{"role": m.role, "content": m.content} for m in history_messages]

                # 6a. Build the SEARCH query. Retrieval embeds only the current
                # message, so a referential follow-up ("what about the KL one?")
                # mis-retrieves (report F6). audit Batch B (B4): when the classify
                # step above already produced a condensed query (referential +
                # history present), reuse it instead of paying for a second Haiku
                # call here. Otherwise fall back to the original
                # classify_intent-then-condense sequence for messages that look
                # referential but had no history yet (first-touch conversation).
                # The LLM reply still receives the ORIGINAL user_text + full
                # history either way. Cheap, gated, and fail-soft (returns
                # user_text on any error), so a condensation hiccup never
                # distorts the reply.
                if search_query_pre is not None:
                    search_query = search_query_pre
                else:
                    search_query = user_text
                    if history_dicts and _looks_referential(user_text):
                        search_query = await llm.condense_followup_query(
                            history_dicts, user_text, tenant_id=incoming.tenant_id
                        )

                # 6b. Embed the (possibly condensed) search query.
                query_embedding = await embed.embed_query(
                    search_query, tenant_id=incoming.tenant_id
                )

                # 7. Hybrid RAG with tier-gated rerank. audit Batch B (B1): pass
                # the already-loaded tenant_snapshot's tier fields so this skips
                # BOTH the Redis tier-cache lookup and the `tenants` SELECT
                # rag._get_tenant_tier would otherwise issue.
                # audit Batch B (B2, opt-in): a `simple`-tier message can skip
                # RAG entirely behind `simple_tier_skip_rag` (default off — RAG
                # behavior is unchanged unless the flag is explicitly enabled).
                skip_rag = intent == "simple" and get_settings().simple_tier_skip_rag
                if skip_rag:
                    chunks: list[Any] = []
                else:
                    chunks = await rag.search_hybrid(
                        conn=conn,
                        tenant_id=incoming.tenant_id,
                        query_text=search_query,
                        query_embedding=query_embedding,
                        redis=deps.redis,
                        tier_info=rag.TenantTierInfo(
                            plan_tier=tenant_snapshot.get("plan_tier") or "starter",
                            cohere_rerank_enabled=tenant_snapshot.get("cohere_rerank_enabled"),
                        ),
                    )

                # 7a. Catalogue products (report F8) + active promotions (F7).
                # Previously neither was consulted on the default reply path —
                # "how much is X" only worked if the price was also in a chunk,
                # and promotions had no bot path at all. Products are RELEVANCE-
                # GATED on the vector distance (migration 113) so unrelated turns
                # ("what time do you open?") inject nothing; promotions are
                # injected when active and the prompt restrains mentioning them
                # to discount/offer questions. Reuses the query embedding — one
                # extra hybrid SQL + one cheap SELECT per inbound. Fail-soft:
                # a catalogue/promo read must never block the KB-driven reply.
                # audit Batch B (B2): skip both lookups entirely for `simple`
                # greetings/small-talk — no RPC/SELECT issued, matching the
                # handoff branch above which already skips everything.
                product_hits = []
                active_promos = []
                if intent != "simple":
                    try:
                        raw_products = await products_search.search_products_hybrid(
                            conn,
                            incoming.tenant_id,
                            search_query,
                            query_embedding,
                            tenant_branch_id=conversation_branch_id,
                        )
                        # Only inject products the vector leg places near the query
                        # (< _PRODUCT_RELEVANCE_MAX_DISTANCE) or that matched lexically
                        # (BM25-only hit ⇒ distance None ⇒ keep). Keeps the bot from
                        # volunteering products on unrelated questions.
                        product_hits = [
                            p for p in raw_products if product_passes_relevance_gate(p.distance)
                        ]
                    except Exception:  # noqa: BLE001 — catalogue lookup is best-effort
                        logger.warning(
                            "pipeline: product lookup failed tenant=%s — reply continues",
                            incoming.tenant_id,
                            exc_info=True,
                        )
                    try:
                        active_promos = await promotions_lookup.fetch_active_promotions(
                            conn,
                            incoming.tenant_id,
                            tenant_branch_id=conversation_branch_id,
                        )
                    except Exception:  # noqa: BLE001 — promo lookup is best-effort
                        logger.warning(
                            "pipeline: promo lookup failed tenant=%s — reply continues",
                            incoming.tenant_id,
                            exc_info=True,
                        )

                # 9a. Build tenant persona from the top-level tenant_snapshot
                # (audit Batch B / B1 — loaded once near the top of _run_inner;
                # this branch no longer issues its own duplicate
                # `load_tenant_snapshot` call). H-3 audit fix (2026-05-19):
                # uses tenants.system_prompt + settings JSONB so each tenant's
                # brand voice is honoured instead of every tenant getting
                # "Pol from Polaris".
                persona = TenantPersona(
                    assistant_name=tenant_snapshot.get("persona_assistant_name"),
                    company_name=tenant_snapshot.get("persona_company_name"),
                    system_prompt_override=tenant_snapshot.get("system_prompt"),
                    persona_blocks=coerce_persona_blocks(tenant_snapshot.get("persona_blocks")),
                )

                # 9b. Generate response. Context = KB chunks + relevant catalogue
                # products (F8) + active promotions (F7), joined into one block.
                rag_context = "\n\n".join(
                    block
                    for block in (
                        format_rag_context(chunks),
                        format_products_context(product_hits),
                        format_promotions_context(active_promos),
                    )
                    if block
                )
                response = await llm.generate_response(
                    tenant_id=incoming.tenant_id,
                    rag_context=rag_context,
                    conversation_history=history_dicts,
                    user_message=user_text,
                    intent=intent,
                    chunks=chunks,
                    persona=persona,
                    channel=channel,
                )

                # 9c. Retrieval-confidence miss signal — computed ONCE here,
                # before persist, so it drives BOTH the knowledge-gap row (11b)
                # and the opt-in escalation offer. Cosine-similarity based (F3).
                is_miss, miss_signal, top_score = knowledge_gap_capture.classify_confidence_miss(
                    chunks,
                    get_settings().knowledge_gap_min_similarity,
                )
                # Opt-in escalation: track consecutive unhelpful replies; on the
                # 2nd, append an offer to connect to the team and arm consent so
                # the next turn's "yes" escalates (handled at the top of run()).
                esc_state, _should_offer = escalation.register_reply(
                    esc_state, is_unhelpful=is_miss
                )
                if _should_offer:
                    response.content = (
                        response.content.rstrip()
                        + "\n\n"
                        + escalation.offer_text(response.language_detected)
                    )
                await _save_escalation_state(deps.redis, conversation_id, esc_state)

                # 10. Persist assistant message with full telemetry.
                assistant_msg_id = await _persist_assistant_message(
                    conn=conn,
                    redis=deps.redis,
                    tenant_id=incoming.tenant_id,
                    conversation_id=conversation_id,
                    response=response,
                    chunks=chunks,
                    response_time_ms=int((time.monotonic() - t0) * 1000),
                )

                # 11. Bump last_message_at.
                await _bump_last_message_at(conn, conversation_id, incoming.tenant_id)

                # 11a. Follow-up: bot replied last on a general-support turn
                # (not a flow/booking/handoff path) → schedule one humane
                # re-engagement nudge if the conversation qualifies. Inside
                # the tx so the GUC scopes the write; the next inbound (or
                # handoff) cancels it.
                await followup_scheduler.maybe_schedule_followup(
                    conn, tenant_id=incoming.tenant_id, conversation_id=conversation_id
                )

                # 11b. Knowledge Gap Detection (M1): if RAG surfaced nothing
                # useful, this reply is a knowledge-gap miss (the bot answered
                # from persona/history alone, or couldn't). Fail-soft; Ultra-
                # gated via capture_ok. Inside the tx so the GUC scopes the write.
                # Uses the confidence miss computed at 9c (F3). Ultra-gated.
                # audit Batch B (B2): when RAG was deliberately skipped
                # (`simple_tier_skip_rag`), an empty chunk list is NOT a
                # knowledge-gap miss — it's "we didn't look", not "we looked
                # and found nothing". Skip the capture write in that case.
                if not skip_rag and capture_ok and is_miss and miss_signal is not None:
                    await knowledge_gap_capture.record_miss(
                        conn,
                        tenant_id=incoming.tenant_id,
                        conversation_id=conversation_id,
                        message_id=user_msg_id,
                        customer_question=user_text,
                        signal_type=miss_signal,
                        retrieval_top_score=top_score,
                        channel=channel,
                        detected_language=response.language_detected,
                    )

                handoff_emitted = False
                rag_chunks_for_decision = chunks

    # 12. Dispatch outbound — OUTSIDE the transaction so a Meta blip
    # doesn't roll back the committed messages row.
    provider_message_id = await _dispatch_reply(
        deps=deps,
        tenant_id=incoming.tenant_id,
        channel=channel,
        channel_user_id=incoming.sender.channel_user_id,
        response=response,
        channel_id=incoming.channel_id,
        assistant_msg_id=assistant_msg_id,
    )

    # 12a. Mirror bot reply into CW conversation (best-effort).
    # Makes the reply visible to agents in the CW UI and satisfies
    # pollCwBotReply() in Phase 12 E2E specs.  Runs only when
    # _link_cw_conversation() linked a conversation above.
    if _cw_account_id and _cw_conv_id and response and response.content:
        await _mirror_reply_to_cw(
            cw_account_id=_cw_account_id,
            cw_conv_id=_cw_conv_id,
            response=response,
            provider_message_id=provider_message_id,
            tenant_id=incoming.tenant_id,
        )

    # 12b. Increment bot-reply Redis counter (Phase 10 PR-1 — ADR-017).
    # Shared helper so the classic, flow, and agent paths count identically.
    await _increment_bot_reply_quota(
        deps,
        tenant_id=incoming.tenant_id,
        provider_message_id=provider_message_id,
        demo=demo,
    )

    # 13. Best-effort: stamp provider_message_id on the assistant row
    # for outbound dedup correlation. Failure here is logged but
    # doesn't propagate — the user already got the reply.
    if provider_message_id is not None:
        await _stamp_provider_message_id(
            deps=deps,
            tenant_id=incoming.tenant_id,
            assistant_msg_id=assistant_msg_id,
            provider_message_id=provider_message_id,
        )

    _ = rag_chunks_for_decision  # reserved for future PipelineDecision fields
    return PipelineDecision(
        conversation_id=conversation_id,
        tenant_id=incoming.tenant_id,
        user_message_id=user_msg_id,
        assistant_message_id=assistant_msg_id,
        response=response,
        provider_message_id=provider_message_id,
        handoff_emitted=handoff_emitted,
    )


async def _increment_bot_reply_quota(
    deps: PipelineDeps,
    *,
    tenant_id: UUID,
    provider_message_id: str | None,
    demo: bool,
) -> None:
    """Increment the tenant's bot-reply Redis counter (Phase 10 PR-1 — ADR-017).

    Only counts when a reply was actually dispatched (``provider_message_id``
    returned). Best-effort: a Redis hiccup must not roll back the committed
    message row. ADR-066: brand-demo replies do not consume the tenant's
    bot-reply quota.

    Shared by the classic, flow, and agent reply paths so all three count
    identically. The flow/agent paths return early from ``_run_inner`` and
    previously skipped this counter — a quota undercount that let flow- and
    agent-mode tenants exceed their plan without ``over_cap`` ever firing.
    """
    if provider_message_id is None or demo:
        return
    try:
        await quota.increment_counter(deps.redis, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "pipeline.quota_incr: Redis increment failed tenant=%s — "
            "counter will be reconciled by nightly flush",
            tenant_id,
        )


async def _try_agent_runtime(
    *,
    conn: asyncpg.Connection,
    deps: PipelineDeps,
    incoming: IncomingMessage,
    conversation_id: UUID,
    user_msg_id: UUID,
    t0: float,
    channel: str,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
    snapshot: dict[str, Any] | None = None,
    tenant_branch_id: UUID | None = None,
) -> PipelineDecision | None:
    """Agent tool-surface branch (ADR-063).

    Returns a ``PipelineDecision`` when the tenant has the agent enabled and it
    produced a reply, or ``None`` when the agent is disabled (fall through to the
    flow / classic path). Persist + dispatch use the same helpers as Phase 4, so
    the customer-facing shape is identical.

    ``snapshot`` (audit Batch B / B1): the caller's already-loaded tenant
    snapshot, forwarded to ``try_run_agent`` so it doesn't re-read `tenants`.
    """
    try:
        result = await agent_integration.try_run_agent(
            conn=conn,
            redis=deps.redis,
            meta_client=deps.meta_client,
            incoming=incoming,
            conversation_id=conversation_id,
            channel=channel,
            snapshot=snapshot,
            tenant_branch_id=tenant_branch_id,
        )
    except LLMHardFailure:
        # Both Anthropic AND OpenRouter failed inside the agent tool loop.
        # Fall through to the flow / classic reply path (same contract as the
        # agent-disabled case, result=None) rather than dropping the message.
        logger.warning(
            "pipeline: agent runtime hit LLMHardFailure tenant=%s conv=%s — "
            "falling through to flow/classic path",
            incoming.tenant_id,
            conversation_id,
        )
        return None
    if result is None:
        return None

    response = result.envelope
    assistant_msg_id = await _persist_assistant_message(
        conn=conn,
        redis=deps.redis,
        tenant_id=incoming.tenant_id,
        conversation_id=conversation_id,
        response=response,
        chunks=[],
        response_time_ms=int((time.monotonic() - t0) * 1000),
        flow_run_id=None,
    )
    await _bump_last_message_at(conn, conversation_id, incoming.tenant_id)

    provider_message_id = await _dispatch_reply(
        deps=deps,
        tenant_id=incoming.tenant_id,
        channel=channel,
        channel_user_id=incoming.sender.channel_user_id,
        response=response,
        channel_id=incoming.channel_id,
        assistant_msg_id=assistant_msg_id,
    )
    if provider_message_id is not None:
        await _stamp_provider_message_id(
            deps=deps,
            tenant_id=incoming.tenant_id,
            assistant_msg_id=assistant_msg_id,
            provider_message_id=provider_message_id,
        )

    if cw_account_id and cw_conv_id and response and response.content:
        await _mirror_reply_to_cw(
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
            response=response,
            provider_message_id=provider_message_id,
            tenant_id=incoming.tenant_id,
        )

    return PipelineDecision(
        conversation_id=conversation_id,
        tenant_id=incoming.tenant_id,
        user_message_id=user_msg_id,
        assistant_message_id=assistant_msg_id,
        response=response,
        provider_message_id=provider_message_id,
        handoff_emitted="handoff_to_human" in result.tools_invoked,
    )


async def _try_booking_command(
    *,
    conn: asyncpg.Connection,
    deps: PipelineDeps,
    incoming: IncomingMessage,
    conversation_id: UUID,
    user_msg_id: UUID,
    t0: float,
    channel: str,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
) -> PipelineDecision | None:
    """Conversational cancel / reschedule / confirm / book branch (v2.2).

    Returns:
      * ``PipelineDecision`` — the message was a booking command and we
        acted (cancel / reschedule link / confirm ack / new-booking link /
        in-chat slot pick/confirm), persisted + dispatched the reply, and
        the caller returns this unchanged.
      * ``None`` — not a booking command, or no booking on file for
        cancel/reschedule/confirm; the pipeline falls through to the flow
        runtime / Phase 4 path.

    Phase 2: if an in-chat slot-picker is active for this conversation
    (``BookingPickerState`` in Redis), the customer's message is routed
    into the picker flow PRE-AGENT — before intent classification — so
    the agent never steals the customer's "yes" / slot reply.

    Mirrors ``_try_agent_runtime``'s persist→dispatch→mirror shape so the
    outbound is identical from the customer's POV. Booking commands are
    transactional (not LLM replies), so they are NOT counted against the
    bot-reply quota.
    """
    # Phase 2: load any active in-chat picker state PRE-AGENT.
    picker_state = await _load_booking_picker_state(deps.redis, conversation_id)

    # Phase 2 (details capture): hand the picker a Chatwoot client + the CW
    # conversation/account ids so it can PREFILL the customer's phone/email from
    # the contact (only fetched once, on entry to the details step). When the
    # conversation isn't linked to Chatwoot, the picker just asks for everything.
    cw_client: object | None = None
    if cw_account_id is not None and cw_conv_id is not None:
        from polaris_api.services.chatwoot.client import get_chatwoot_client

        cw_client = get_chatwoot_client()

    reply = await booking_conversational.handle_booking_command(
        conn,
        tenant_id=incoming.tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        customer_handle=incoming.sender.channel_user_id,
        text=incoming.body or "",
        picker_state=picker_state,
        chatwoot=cw_client,
        cw_account_id=cw_account_id,
        cw_conv_id=cw_conv_id,
    )
    if reply is None:
        return None

    logger.info(
        "pipeline.booking_command tenant=%s conv=%s kind=%s",
        incoming.tenant_id,
        conversation_id,
        reply.kind,
    )

    # Phase 2: the handler's reply is the single source of truth for picker
    # state — an in-chat offer/confirm carries the next state to persist, every
    # other reply (booked, error, link fallback, cancel/reschedule/confirm)
    # carries None, which clears the key. So one unconditional save suffices.
    await _save_booking_picker_state(deps.redis, conversation_id, reply.new_picker_state)

    response = _templated_response(reply.content)
    assistant_msg_id = await _persist_no_llm_message(
        conn=conn,
        redis=deps.redis,
        tenant_id=incoming.tenant_id,
        conversation_id=conversation_id,
        content=reply.content,
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
    await _bump_last_message_at(conn, conversation_id, incoming.tenant_id)

    provider_message_id = await _dispatch_reply(
        deps=deps,
        tenant_id=incoming.tenant_id,
        channel=channel,
        channel_user_id=incoming.sender.channel_user_id,
        response=response,
        channel_id=incoming.channel_id,
        assistant_msg_id=assistant_msg_id,
    )
    if provider_message_id is not None:
        await _stamp_provider_message_id(
            deps=deps,
            tenant_id=incoming.tenant_id,
            assistant_msg_id=assistant_msg_id,
            provider_message_id=provider_message_id,
        )

    # Mirror the reply into CW so agents see it (best-effort).
    if cw_account_id and cw_conv_id and response.content:
        await _mirror_reply_to_cw(
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
            response=response,
            provider_message_id=provider_message_id,
            tenant_id=incoming.tenant_id,
        )

    return PipelineDecision(
        conversation_id=conversation_id,
        tenant_id=incoming.tenant_id,
        user_message_id=user_msg_id,
        assistant_message_id=assistant_msg_id,
        response=response,
        provider_message_id=provider_message_id,
        handoff_emitted=False,
    )


async def _ensure_conversation(
    *,
    conn: asyncpg.Connection,
    tenant_id: UUID,
    channel: str,
    channel_user_id: str,
    channel_id: UUID | None = None,
) -> tuple[UUID, UUID | None]:
    """Race-safe conversation upsert via INSERT ... ON CONFLICT DO NOTHING.

    Returns ``(conversations.id, tenant_branch_id)`` — the branch is projected
    from both the INSERT RETURNING and the conflict-path SELECT (both already
    touch this row) so callers scope branch-aware lookups without a third query.
    The unique index in migration 026
    (``tenant_id, channel, channel_user_id``) makes the upsert
    deterministic — two inbound webhooks landing simultaneously
    can't fork the conversation row.

    Two-roundtrip pattern (per code-review L-1 fix): INSERT with
    DO NOTHING returns NULL on conflict; we then SELECT the existing
    id. Avoids the ``DO UPDATE SET tenant_id = EXCLUDED.tenant_id``
    no-op write lock that would otherwise fire under burst from the
    same user (multiple inbound messages arriving simultaneously).

    Branch stamping (v2.1): when ``channel_id`` (the specific inbox) is
    supplied, copy its ``tenant_branch_id`` onto the new conversation so
    downstream nodes (e.g. branch-centric EA booking routing) know the
    branch without re-resolving the inbox. The scalar subquery yields
    NULL when channel_id is None or the inbox has no branch — preserving
    GA behaviour. Only stamped at creation; an existing conversation
    keeps its branch (ON CONFLICT DO NOTHING).

    Channel pinning (migration 106): ``channel_id`` is ALSO persisted on the
    conversation so the human-agent forward path
    (``webhook_resolver.resolve_agent_forward_context``) can reply from the
    SAME WABA the customer messaged (ADR-015 multiplicity), instead of guessing
    among a multi-WABA tenant's rows. NULL for conversations created before the
    column landed; the resolver falls back to its prior ordering then.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO conversations
            (tenant_id, channel, channel_user_id, channel_id, tenant_branch_id)
        VALUES (
            $1, $2, $3, $4,
            (SELECT tenant_branch_id FROM tenant_channels WHERE id = $4)
        )
        ON CONFLICT (tenant_id, channel, channel_user_id) DO NOTHING
        RETURNING id, tenant_branch_id
        """,
        tenant_id,
        channel,
        channel_user_id,
        channel_id,
    )
    if row is None:
        # Conflict path — row already existed; fetch its id.
        row = await conn.fetchrow(
            """
            SELECT id, tenant_branch_id FROM conversations
             WHERE tenant_id = $1 AND channel = $2 AND channel_user_id = $3
            """,
            tenant_id,
            channel,
            channel_user_id,
        )
    if row is None:  # pragma: no cover — INSERT + SELECT can't both miss
        raise RuntimeError("_ensure_conversation: no row returned")
    new_id: UUID = row["id"]
    branch_id: UUID | None = row["tenant_branch_id"]
    return new_id, branch_id


async def _link_cw_conversation(
    *,
    conn: asyncpg.Connection,
    tenant_id: UUID,
    conversation_id: UUID,
    incoming: IncomingMessage,
) -> tuple[int | None, int | None]:
    """Link the Polaris conversation to its EXISTING Chatwoot conversation.

    Chatwoot-native ingress: the agent-bot webhook already carries the
    Chatwoot conversation + account ids (``incoming.chatwoot_*``). Chatwoot
    owns conversation creation, so we never create one — that produced a
    duplicate CW conversation alongside the one the webhook fired from. We
    persist the ids onto the ``conversations`` row (idempotent UPDATE) so the
    handoff / label / reply-mirror / recovery posts target the real
    conversation.

    Returns ``(cw_account_id, cw_conversation_id)`` for those posts, or
    ``(None, None)`` when the inbound carries no Chatwoot linkage (non-Chatwoot
    ingress — nothing to mirror). Called inside the active DB transaction; the
    UPDATE lands atomically with the rest of the pipeline's DB work.
    """
    cw_conv_id = incoming.chatwoot_conversation_id
    cw_account_id = incoming.chatwoot_account_id
    if cw_conv_id is None:
        return None, None
    if cw_account_id is None:
        # A Chatwoot payload that carries conversation.id but not account.id is
        # malformed/unexpected. Every downstream CW op gates on BOTH ids, so
        # they would all silently no-op (no bot-handling label, no reply
        # mirror, no quota escalation). Surface it rather than skip silently.
        logger.warning(
            "pipeline._link_cw_conversation: cw_conv_id=%s present but "
            "account_id absent — CW label/mirror/escalation skipped tenant=%s",
            cw_conv_id,
            tenant_id,
        )
    await conn.execute(
        """
        UPDATE conversations
           SET chatwoot_conversation_id = $1
         WHERE id = $2 AND tenant_id = $3
           AND chatwoot_conversation_id IS DISTINCT FROM $1
        """,
        cw_conv_id,
        conversation_id,
        tenant_id,
    )
    return cw_account_id, cw_conv_id


async def _persist_assistant_message(
    *,
    conn: asyncpg.Connection,
    redis: redis_async.Redis,
    tenant_id: UUID,
    conversation_id: UUID,
    response: ResponseEnvelope,
    chunks: list[Any],
    response_time_ms: int,
    flow_run_id: UUID | None = None,
) -> UUID:
    """Write assistant message with all 11 telemetry fields populated.

    Per deep-dive §10.2 Phase 4 exit criterion: every assistant row
    must have model_used, input_tokens, output_tokens, cache_read_tokens
    populated. NULL fields here mean a Phase 9 dashboard query goes
    blank — caught by test_messages_persisted in PR-4.

    Per code-review C-1: ``intent_tier`` (classifier output:
    simple/medium/hard/handoff per migration 030) and ``intent`` (the
    SSOT semantic label populated by Phase 5 flow routing) are
    DIFFERENT columns with DIFFERENT vocabularies. Pre-PR-7 the
    pipeline wrote the tier into the semantic column, which would
    have raised a CHECK violation on every non-handoff reply.
    Phase 4 writes only ``intent_tier``; ``intent`` stays NULL until
    Phase 5 wires real semantic classification.

    Phase 5 PR-4: ``flow_run_id`` is populated when the reply was
    generated by a published flow (via FlowRunner). Column FK was
    added in migration 037 (DEFERRABLE INITIALLY DEFERRED so the
    flow_runs row + this message row can land in any order within
    one transaction).
    """
    rag_chunk_ids = [c.id for c in chunks] if chunks else None
    row = await conn.fetchrow(
        """
        INSERT INTO messages (
            tenant_id, conversation_id, role, content,
            intent_tier, model_used, response_time_ms,
            input_tokens, output_tokens, cache_read_tokens, cached_tokens,
            rag_chunk_ids, token_count, flow_run_id
        ) VALUES (
            $1, $2, 'assistant', $3,
            $4, $5, $6,
            $7, $8, $9, $10,
            $11, $12, $13
        )
        RETURNING id, created_at
        """,
        tenant_id,
        conversation_id,
        response.content,
        response.intent,
        response.model_used,
        response_time_ms,
        response.usage.input_tokens,
        response.usage.output_tokens,
        response.usage.cache_read_tokens,
        response.usage.cache_creation_tokens,
        rag_chunk_ids,
        response.usage.input_tokens + response.usage.output_tokens,
        flow_run_id,
    )
    if row is None:  # pragma: no cover
        raise RuntimeError("_persist_assistant_message: no row returned")
    new_id: UUID = row["id"]

    # Cache-append delegates to the public helper in services/conversation.py
    # (per code-review H-4: no more private-symbol cross-module imports).
    await conversation_service.append_assistant_to_cache(
        redis=redis,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        content=response.content,
        created_at=row["created_at"],
        token_count=response.usage.output_tokens,
    )

    return new_id


async def _bump_last_message_at(
    conn: asyncpg.Connection,
    conversation_id: UUID,
    tenant_id: UUID,
) -> None:
    """Update conversations.last_message_at = now() — tenant-scoped.

    Used by the Phase 8 portal's conversation-list page to sort
    most-recent-first. Failure here is logged-only — sorting drift
    is acceptable; failing the pipeline isn't.

    Per code-review H-3: the WHERE clause filters on BOTH id +
    tenant_id (not just id). RLS already enforces tenant isolation
    via ``app.current_tenant_id`` GUC + the conversations policy,
    so this is defense in depth — a future BYPASSRLS code path that
    forgets to SET LOCAL would still be confined to the intended
    tenant's row by the explicit filter. Zero perf impact (PK
    lookup stays a point lookup).
    """
    await conn.execute(
        """
        UPDATE conversations SET last_message_at = now()
         WHERE id = $1 AND tenant_id = $2
        """,
        conversation_id,
        tenant_id,
    )


async def _persist_handoff_message(
    *,
    conn: asyncpg.Connection,
    redis: redis_async.Redis,
    tenant_id: UUID,
    conversation_id: UUID,
    latency_ms: int,
) -> UUID:
    """Persist the handoff template as an assistant turn.

    Writes BOTH ``intent='handoff'`` (the SSOT semantic label — handoff
    is the one overlap between semantic + tier vocabularies, so it's
    safe to populate here) AND ``intent_tier='handoff'`` (the
    classifier output). model_used NULL because no LLM call was made
    for the response text itself; only the classifier ran. All token
    fields NULL.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO messages (
            tenant_id, conversation_id, role, content,
            intent, intent_tier, response_time_ms
        ) VALUES ($1, $2, 'assistant', $3, 'handoff', 'handoff', $4)
        RETURNING id, created_at
        """,
        tenant_id,
        conversation_id,
        _HANDOFF_REPLY_TEXT,
        latency_ms,
    )
    if row is None:  # pragma: no cover
        raise RuntimeError("_persist_handoff_message: no row returned")
    new_id: UUID = row["id"]
    return new_id


def _build_handoff_response(
    *, text: str | None = None, language: LanguageDetected = "en"
) -> ResponseEnvelope:
    """ResponseEnvelope for the handoff reply — fills the dispatch path.

    L-1 audit fix (2026-05-19): tag as ``model_used="handoff"`` so the
    Phase 9 dashboard's per-model counters don't conflate hardcoded
    handoff templates with real Haiku calls. The classifier (which DID
    run) is tagged separately via ``messages.intent_tier`` ('handoff').
    Migration 040 relaxes the messages.model_used CHECK constraint to
    permit the new value.

    ``text``/``language`` override the default template for the opt-in
    escalation path (report don't-know handling): when the customer accepts the
    bot's offer to connect, we send the language-matched acceptance text instead
    of the generic classifier-handoff line.
    """
    return ResponseEnvelope(
        content=text or _HANDOFF_REPLY_TEXT,
        intent="handoff",
        model_used="handoff",
        language_detected=language,
        usage=UsageMetrics(input_tokens=0, output_tokens=0),
        latency_ms=_HANDOFF_LATENCY_PLACEHOLDER_MS,
    )


_ESC_KEY_PREFIX: Final[str] = "esc:"


_ESC_TTL_SECONDS: Final[int] = 86400  # 24h — the escalation window is short-lived


async def _load_escalation_state(
    redis: redis_async.Redis | None, conversation_id: UUID
) -> escalation.EscalationState:
    """Read the conversation's escalation state from Redis (fail-soft → fresh)."""
    if redis is None:
        return escalation.EscalationState()
    try:
        raw = await redis.get(f"{_ESC_KEY_PREFIX}{conversation_id}")
    except Exception:  # noqa: BLE001 — a Redis hiccup must not break the reply
        return escalation.EscalationState()
    if not raw:
        return escalation.EscalationState()
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, TypeError, AttributeError):
        return escalation.EscalationState()
    return escalation.EscalationState.from_dict(data)


async def _save_escalation_state(
    redis: redis_async.Redis | None,
    conversation_id: UUID,
    state: escalation.EscalationState,
) -> None:
    """Persist escalation state to Redis with a TTL (fail-soft / best-effort)."""
    if redis is None:
        return
    try:
        await redis.set(
            f"{_ESC_KEY_PREFIX}{conversation_id}",
            json.dumps(state.to_dict()),
            ex=_ESC_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 — best-effort; never block the reply
        logger.warning("pipeline: escalation-state save failed conv=%s", conversation_id)


_BOOK_KEY_PREFIX: Final[str] = "book:"


_BOOK_TTL_SECONDS: Final[int] = 1800  # 30 min — picker window is short-lived


async def _load_booking_picker_state(
    redis: redis_async.Redis | None, conversation_id: UUID
) -> BookingPickerState | None:
    """Read the conversation's booking-picker state from Redis (fail-soft → None).

    Returns ``None`` when there is no active picker state (normal — most
    turns), so the caller knows there's nothing to route.
    """
    if redis is None:
        return None
    try:
        raw = await redis.get(f"{_BOOK_KEY_PREFIX}{conversation_id}")
    except Exception:  # noqa: BLE001 — a Redis hiccup must not break the reply
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, TypeError, AttributeError):
        return None
    return BookingPickerState.from_dict(data)


async def _save_booking_picker_state(
    redis: redis_async.Redis | None,
    conversation_id: UUID,
    state: BookingPickerState | None,
) -> None:
    """Persist (or clear) booking-picker state in Redis (fail-soft / best-effort).

    Passing ``state=None`` deletes the key (booking complete / cancelled).
    """
    if redis is None:
        return
    key = f"{_BOOK_KEY_PREFIX}{conversation_id}"
    try:
        if state is None:
            await redis.delete(key)
        else:
            await redis.set(
                key,
                json.dumps(state.to_dict()),
                ex=_BOOK_TTL_SECONDS,
            )
    except Exception:  # noqa: BLE001 — best-effort; never block the reply
        logger.warning("pipeline: booking-picker-state save failed conv=%s", conversation_id)


async def _persist_no_llm_message(
    *,
    conn: asyncpg.Connection,
    redis: redis_async.Redis,
    tenant_id: UUID,
    conversation_id: UUID,
    content: str,
    latency_ms: int,
) -> UUID:
    """Persist a no-LLM templated reply (booking command / voice fallback) as
    an assistant turn.

    No LLM ran, so ``model_used`` / token columns stay NULL (these are
    transactional/templated, not generated replies). ``intent`` /
    ``intent_tier`` also stay NULL — neither vocabulary
    (simple/medium/hard/handoff) describes such an action, and both
    columns are nullable (migrations 028/030). The Redis cache is
    appended so the reply shows up in the conversation window.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO messages (
            tenant_id, conversation_id, role, content, response_time_ms
        ) VALUES ($1, $2, 'assistant', $3, $4)
        RETURNING id, created_at
        """,
        tenant_id,
        conversation_id,
        content,
        latency_ms,
    )
    if row is None:  # pragma: no cover
        raise RuntimeError("_persist_no_llm_message: no row returned")
    new_id: UUID = row["id"]
    await conversation_service.append_assistant_to_cache(
        redis=redis,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        content=content,
        created_at=row["created_at"],
        token_count=0,
    )
    return new_id


def _templated_response(content: str) -> ResponseEnvelope:
    """ResponseEnvelope wrapper for a no-LLM templated reply.

    Used for booking-command replies, voice-note fallbacks, quota-exhaustion
    holding messages, and demo canned messages. Only ``content`` reaches the
    customer; this envelope exists to feed the shared ``_dispatch_reply`` path.
    These messages are NOT persisted via ``_persist_assistant_message``, so the
    ``model_used``/``intent`` tags here never touch the DB — they reuse the
    handoff sentinel values purely to satisfy the envelope schema.
    """
    return ResponseEnvelope(
        content=content,
        intent="handoff",
        model_used="handoff",
        language_detected="en",
        usage=UsageMetrics(input_tokens=0, output_tokens=0),
        latency_ms=_HANDOFF_LATENCY_PLACEHOLDER_MS,
    )


async def _emit_handoff_for_empty_body(
    *,
    incoming: IncomingMessage,
    channel: str,
    deps: PipelineDeps,
    t0: float,
) -> PipelineDecision:
    """Media-only inbound → emit handoff template instead of crashing.

    Phase 4 doesn't process media (audio / image / document). Routing
    these to handoff matches the deep-dive §9.7 "never silent" rule
    without forcing the pipeline to attempt LLM classification on
    empty text.
    """
    async with deps.anon_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(incoming.tenant_id),
            )
            # Media-only path doesn't do branch-scoped lookups; ignore the branch.
            conversation_id, _ = await _ensure_conversation(
                conn=conn,
                tenant_id=incoming.tenant_id,
                channel=channel,
                channel_user_id=incoming.sender.channel_user_id,
                channel_id=incoming.channel_id,
            )
            # Media-only inbound is still customer activity → cancel any
            # pending follow-up (this path emits a handoff, never schedules).
            await followup_scheduler.cancel_pending_followup(
                conn, tenant_id=incoming.tenant_id, conversation_id=conversation_id
            )
            # Save inbound user "[media message]" placeholder so the
            # conversation history shows the gap.
            user_msg_id = await conversation_service.save_message(
                conn=conn,
                redis=deps.redis,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
                role="user",
                content="[media-only message]",
            )
            assistant_msg_id = await _persist_handoff_message(
                conn=conn,
                redis=deps.redis,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            await _bump_last_message_at(conn, conversation_id, incoming.tenant_id)

    response = _build_handoff_response()
    provider_message_id = await _dispatch_reply(
        deps=deps,
        tenant_id=incoming.tenant_id,
        channel=channel,
        channel_user_id=incoming.sender.channel_user_id,
        response=response,
        channel_id=incoming.channel_id,
        assistant_msg_id=assistant_msg_id,
    )
    if provider_message_id is not None:
        await _stamp_provider_message_id(
            deps=deps,
            tenant_id=incoming.tenant_id,
            assistant_msg_id=assistant_msg_id,
            provider_message_id=provider_message_id,
        )

    return PipelineDecision(
        conversation_id=conversation_id,
        tenant_id=incoming.tenant_id,
        user_message_id=user_msg_id,
        assistant_message_id=assistant_msg_id,
        response=response,
        provider_message_id=provider_message_id,
        handoff_emitted=True,
    )


async def _resolve_channel_owner(channel_id: UUID | None) -> UUID | None:
    """Return the tenant that OWNS a channel row (ADR-066 dispatch).

    Uses the BYPASSRLS service pool so it works regardless of which content
    tenant is scoped on the anon pool: a brand-demo turn is scoped to the demo
    tenant, but the channel belongs to the gateway tenant. Returns None when
    ``channel_id`` is None or no row matches — the caller falls back to the
    content tenant, preserving pre-demo behaviour for all normal traffic.
    """
    if channel_id is None:
        return None
    from polaris_api.db.pools import get_service_pool

    try:
        pool = get_service_pool()
    except RuntimeError:
        return None
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT tenant_id FROM tenant_channels WHERE id = $1", channel_id
        )
    return owner if isinstance(owner, UUID) else None


async def run_inbound(*, incoming: IncomingMessage, deps: PipelineDeps) -> PipelineDecision:
    """Demo-aware inbound entry point (ADR-066) — the worker calls THIS.

    Resolves the demo decision for the contact, then either:
      * ACTIVATE / REFUSE / EXPIRE → send a canned message on the gateway channel
        and STOP (no LLM turn). ACTIVATE first creates the session; EXPIRE first
        expires it + flags the lead (Demo System log + Chatwoot private note).
      * CONTINUE_DEMO → run the full pipeline scoped to the BRAND tenant
        (``demo=True`` skips audience+quota), then increment the demo counter.
      * DEFAULT → run the full pipeline scoped to the gateway tenant (normal
        Polaris sales bot).

    ``incoming.tenant_id`` arrives as the gateway tenant (``resolve_chatwoot``);
    CONTINUE rewrites it to the brand tenant for content scoping while keeping
    ``channel_id`` = the gateway channel so dispatch creds resolve via the owner.
    """
    from polaris_api.services import demo_lead, demo_runtime, demo_sessions

    channel = _PROVIDER_TO_CHANNEL.get(incoming.provider)
    cuid = incoming.sender.channel_user_id
    # Gateway = the tenant that OWNS this channel (the operator's real tenant).
    # The default / expire / refuse paths scope to it, so the demo system is a
    # pure OVERLAY: a no-code message is answered by the operator's own bot, a
    # demo code switches content to the brand tenant, and expiry reverts. (A
    # hardcoded gateway would wrongly reroute all of a tenant's traffic.)
    decision = await demo_runtime.resolve(
        body=incoming.body,
        channel_user_id=cuid,
        gateway_tenant_id=incoming.tenant_id,
    )

    # --- Direct-message + STOP outcomes (no pipeline LLM turn) ---------------
    if decision.mode in ("activate", "refuse", "expire") and channel is not None:
        if decision.mode == "activate" and decision.demo is not None:
            await demo_sessions.activate_demo_session(
                channel=channel,
                channel_user_id=cuid,
                channel_user_name=incoming.sender.display_name,
                demo=decision.demo,
                chatwoot_conversation_id=incoming.chatwoot_conversation_id,
                chatwoot_inbox_id=None,
            )
        elif decision.mode == "expire" and decision.session is not None:
            await demo_sessions.expire_demo_session(decision.session.session_id)
            from polaris_api.services.chatwoot.client import (
                get_chatwoot_client as _cw,
            )

            brand = (
                decision.session.demo_code if decision.demo is None else decision.demo.display_name
            )
            await demo_lead.flag(
                session_id=decision.session.session_id,
                brand_name=brand,
                cw_client=_cw(),
                cw_account_id=incoming.chatwoot_account_id,
                cw_conversation_id=incoming.chatwoot_conversation_id,
            )

        if decision.direct_message:
            await _dispatch_reply(
                deps=deps,
                tenant_id=incoming.tenant_id,  # gateway owns the channel + creds
                channel=channel,
                channel_user_id=cuid,
                response=_templated_response(decision.direct_message),
                channel_id=incoming.channel_id,
            )
        return PipelineDecision(
            conversation_id=UUID(int=0),
            tenant_id=incoming.tenant_id,
            user_message_id=UUID(int=0),
        )

    # --- Pipeline outcomes (default / continue_demo) -------------------------
    content_incoming = (
        incoming
        if decision.content_tenant_id == incoming.tenant_id
        else incoming.model_copy(update={"tenant_id": decision.content_tenant_id})
    )
    result = await run(incoming=content_incoming, deps=deps, demo=decision.brand_demo)

    if decision.mode == "continue_demo" and decision.session is not None:
        await demo_sessions.increment_demo_message(decision.session.session_id)
    return result


async def _dispatch_reply(
    *,
    deps: PipelineDeps,
    tenant_id: UUID,
    channel: str,
    channel_user_id: str,
    response: ResponseEnvelope,
    channel_id: UUID | None = None,
    assistant_msg_id: UUID | None = None,
) -> str | None:
    """Send reply (text + optional product images) via reply_dispatch.

    Phase 15: resolves the per-tenant Page token (IG/Messenger) and mints
    short-TTL signed read URLs for any product images BEFORE handing off to the
    pure dispatch layer. Runs OUTSIDE the pipeline transaction, so credential
    resolution opens its own short transaction.

    ``channel_id`` is the inbound ``tenant_channels.id`` (ADR-015 multiplicity);
    it pins WhatsApp credential resolution to the SAME WABA the customer
    messaged, not an arbitrary row. ``assistant_msg_id`` lets a send failure be
    stamped on the committed message row.

    On a send failure the message row is already committed, so instead of
    silently dropping the reply this records a durable, visible failure signal
    (``polaris_outbound_dispatch_failures`` metric + ``metadata.dispatch`` on
    the message row) and returns ``None`` — ERROR for permanent failures
    (missing creds / 4xx), WARNING for transient (5xx / network, redelivery
    covers it). The always-200-to-webhook contract is unaffected (dispatch runs
    in the inbound worker, after the webhook already acked).
    """
    image_urls = (
        await _mint_product_image_urls(response.images, tenant_id=tenant_id)
        if response is not None and response.images
        else []
    )
    # ADR-066: resolve send credentials against the channel's OWNER tenant, not
    # the content tenant. For normal traffic the owner IS the content tenant
    # (no behaviour change); for a brand-demo turn the content is the hidden
    # demo tenant but the channel + its creds belong to the gateway tenant, so
    # creds must come from the owner. Falls back to the content tenant when the
    # owner can't be resolved (e.g. channel_id is None) — pre-demo behaviour.
    cred_tenant_id = await _resolve_channel_owner(channel_id) or tenant_id
    page_token = (
        await _resolve_secret_token(deps, cred_tenant_id, "meta_fb_page_access_token")
        if channel in ("messenger",)
        else None
    )
    telegram_token = (
        await _resolve_secret_token(deps, cred_tenant_id, "telegram_bot_token")
        if channel == "telegram"
        else None
    )
    ig_login_token = (
        await _resolve_secret_token(deps, cred_tenant_id, "meta_ig_login_access_token")
        if channel == "instagram_login"
        else None
    )
    whatsapp_pid, whatsapp_token = (
        await _resolve_whatsapp_credentials(deps, cred_tenant_id, channel_id)
        if channel == "whatsapp"
        else (None, None)
    )
    try:
        return await reply_dispatch.send_reply(
            conversation=ConversationRef(
                channel=channel,  # type: ignore[arg-type]
                channel_user_id=channel_user_id,
            ),
            response=response,
            meta_client=deps.meta_client,
            messaging_client=get_meta_messaging_client(),
            page_access_token=page_token,
            whatsapp_phone_number_id=whatsapp_pid,
            whatsapp_access_token=whatsapp_token,
            telegram_client=get_telegram_client() if channel == "telegram" else None,
            telegram_token=telegram_token,
            instagram_login_client=(
                get_instagram_login_client() if channel == "instagram_login" else None
            ),
            ig_login_access_token=ig_login_token,
            image_urls=image_urls,
        )
    except DispatchSkipped as exc:
        # Missing client / token / credentials — a permanent misconfig the
        # operator must fix; no redelivery will ever land this reply.
        logger.error(
            "pipeline.dispatch permanent failure channel=%s reason=%s tenant=%s — "
            "reply NOT sent; recorded on message row + "
            "polaris_outbound_dispatch_failures metric",
            exc.channel,
            exc.reason,
            tenant_id,
        )
        await _record_dispatch_failure(
            deps=deps,
            tenant_id=tenant_id,
            channel=exc.channel,
            reason=exc.reason,
            kind="permanent",
            assistant_msg_id=assistant_msg_id,
        )
        return None
    except (MetaWhatsAppError, MetaMessagingError, TelegramError) as exc:
        # Provider HTTP error: 4xx = permanent (bad request / auth — operator
        # action); 5xx (and Telegram's status_code=0 network wrap) = transient,
        # redelivery covers it.
        permanent = 400 <= exc.status_code < 500
        kind: Literal["permanent", "transient"] = "permanent" if permanent else "transient"
        log = logger.error if permanent else logger.warning
        log(
            "pipeline.dispatch %s failure channel=%s status=%d tenant=%s — reply "
            "NOT sent; recorded on message row + dispatch-failures metric",
            kind,
            channel,
            exc.status_code,
            tenant_id,
        )
        await _record_dispatch_failure(
            deps=deps,
            tenant_id=tenant_id,
            channel=channel,
            reason=f"http_{exc.status_code}",
            kind=kind,
            assistant_msg_id=assistant_msg_id,
        )
        return None
    except httpx.TransportError as exc:
        # Connection / timeout reaching the provider — the Meta clients don't
        # wrap these (Telegram wraps its own into TelegramError, caught above),
        # so catch them here as transient; redelivery covers it. Scoped to
        # TransportError (connect/read/timeout), NOT the full HTTPError tree, so
        # a stray HTTPStatusError can't be mis-tagged transient.
        logger.warning(
            "pipeline.dispatch transient network failure channel=%s tenant=%s "
            "err=%s — reply NOT sent; recorded + retryable",
            channel,
            tenant_id,
            type(exc).__name__,
        )
        await _record_dispatch_failure(
            deps=deps,
            tenant_id=tenant_id,
            channel=channel,
            reason="network",
            kind="transient",
            assistant_msg_id=assistant_msg_id,
        )
        return None
    except (RuntimeError, LLMHardFailure) as exc:
        # RuntimeError covers placeholder-creds dispatch attempts; LLMHardFailure
        # shouldn't propagate this far but defensive. Treat as permanent misconfig.
        logger.error(
            "pipeline.dispatch unrecoverable channel=%s tenant=%s err=%s — reply "
            "NOT sent; recorded on message row + dispatch-failures metric",
            channel,
            tenant_id,
            type(exc).__name__,
        )
        await _record_dispatch_failure(
            deps=deps,
            tenant_id=tenant_id,
            channel=channel,
            reason="runtime_error",
            kind="permanent",
            assistant_msg_id=assistant_msg_id,
        )
        return None


async def _record_dispatch_failure(
    *,
    deps: PipelineDeps,
    tenant_id: UUID,
    channel: str,
    reason: str,
    kind: Literal["permanent", "transient"],
    assistant_msg_id: UUID | None,
) -> None:
    """Record a durable, visible signal that an outbound reply was NOT sent.

    Two surfaces so the loss is never silent:
      * ``polaris_outbound_dispatch_failures`` OTel counter (→ Grafana) —
        always emitted, tagged tenant/channel/reason/kind.
      * the assistant message row's ``metadata.dispatch`` (when the row id is
        known) — a durable per-message marker an operator can query.

    Best-effort: a recording failure must not crash dispatch — the customer
    already didn't get the reply; we don't want to also abort the turn.
    """
    metrics.record_dispatch_failure(tenant_id=tenant_id, channel=channel, reason=reason, kind=kind)
    if assistant_msg_id is None:
        return
    try:
        async with deps.anon_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )
            await conn.execute(
                """
                UPDATE messages
                   SET metadata = metadata || jsonb_build_object(
                       'dispatch', jsonb_build_object(
                           'status', 'failed',
                           'reason', $1::text,
                           'kind', $2::text,
                           'channel', $3::text,
                           'failed_at', to_jsonb(now())
                       ))
                 WHERE id = $4 AND tenant_id = $5
                """,
                reason,
                kind,
                channel,
                assistant_msg_id,
                tenant_id,
            )
    except Exception as exc:  # noqa: BLE001 — recording must not crash dispatch
        logger.warning(
            "pipeline: failed to stamp dispatch failure on msg=%s tenant=%s err=%s",
            assistant_msg_id,
            tenant_id,
            type(exc).__name__,
        )


async def _resolve_secret_token(deps: PipelineDeps, tenant_id: UUID, kind: str) -> str | None:
    """Decrypt a per-tenant single-value send token from ``tenant_secrets``.

    Opens its own short transaction on the anon pool (dispatch runs after the
    main pipeline transaction has closed) so the RLS GUC applies to the
    ``tenant_secrets`` read of the given ``kind``. Returns None when absent / on
    any failure so a missing token can't crash dispatch.
    """
    from polaris_api.services.secrets import get as secrets_get

    try:
        async with deps.anon_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )
            token_bytes = await secrets_get(conn, tenant_id, kind)
    except Exception:  # noqa: BLE001 — missing token must not crash dispatch
        logger.warning("pipeline: failed to resolve %s token for tenant=%s", kind, tenant_id)
        return None
    if token_bytes is None:
        return None
    try:
        return token_bytes.decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


async def _resolve_whatsapp_credentials(
    deps: PipelineDeps, tenant_id: UUID, channel_id: UUID | None = None
) -> tuple[str | None, str | None]:
    """Resolve the per-tenant WhatsApp send credentials.

    Returns ``(phone_number_id, access_token)`` — the ``phone_number_id`` from
    the SPECIFIC ``tenant_channels`` row the inbound message arrived on
    (``channel_id``) and the decrypted ``meta_waba_access_token`` secret.
    Keying on ``channel_id`` (ADR-015 multiplicity) means a multi-WABA tenant
    replies from the SAME number the customer messaged; the old
    ``WHERE channel='whatsapp'`` with no row selector picked an arbitrary WABA.
    Mirrors ``_resolve_secret_token``: opens its own short transaction on the
    anon pool (dispatch runs after the main pipeline transaction closes) so the
    RLS GUC applies to both reads.

    Returns ``(None, None)`` when ``channel_id`` is unknown, either piece is
    missing, or on any failure — the caller then surfaces a permanent failure
    (it does NOT fall back to the shared demo number). Both are returned
    together so a ``phone_number_id`` is never sent without its matching token
    (which ``MetaWhatsAppClient._resolve_token`` would reject).
    """
    from polaris_api.services.secrets import get as secrets_get

    if channel_id is None:
        # Inbound always carries channel_id; its absence means the caller can't
        # pin the WABA, so we can't safely pick one. Surface rather than guess.
        logger.warning(
            "pipeline: whatsapp credential resolution called without channel_id "
            "for tenant=%s — cannot pin WABA, treating as unresolved",
            tenant_id,
        )
        return (None, None)

    try:
        async with deps.anon_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )
            phone_number_id = await conn.fetchval(
                "SELECT phone_number_id FROM tenant_channels "
                "WHERE id = $1 AND tenant_id = $2 AND channel = 'whatsapp'",
                channel_id,
                tenant_id,
            )
            token_bytes = await secrets_get(conn, tenant_id, "meta_waba_access_token")
    except Exception:  # noqa: BLE001 — missing creds must not crash dispatch
        logger.warning(
            "pipeline: failed to resolve whatsapp credentials for tenant=%s",
            tenant_id,
        )
        return (None, None)

    if not phone_number_id or token_bytes is None:
        return (None, None)
    try:
        return (str(phone_number_id), token_bytes.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return (None, None)


async def _stamp_provider_message_id(
    *,
    deps: PipelineDeps,
    tenant_id: UUID,
    assistant_msg_id: UUID,
    provider_message_id: str,
) -> None:
    """Best-effort: stamp wamid on the assistant row for dedup correlation."""
    try:
        async with deps.anon_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_tenant_id', $1, true)",
                    str(tenant_id),
                )
                await conn.execute(
                    "UPDATE messages SET provider_message_id = $1 WHERE id = $2 AND tenant_id = $3",
                    provider_message_id,
                    assistant_msg_id,
                    tenant_id,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pipeline stamp provider_message_id failed msg=%s err=%s",
            assistant_msg_id,
            type(exc).__name__,
        )
