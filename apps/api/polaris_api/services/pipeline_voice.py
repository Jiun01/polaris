"""Voice-note handling — extracted from pipeline.py (Batch E, task E1; see
PRP plan audit-batch-e-structural-splits).

Tier-gate -> download -> transcribe one inbound voice note
(``_handle_inbound_voice_note``), plus the Starter-gate / transcription-
failure fallback reply (``_emit_voice_fallback``). Called once from
``pipeline._run_inner``.

``_emit_voice_fallback`` calls back into pipeline.py's persistence/dispatch
helpers (``_ensure_conversation``, ``_link_cw_conversation``,
``_persist_no_llm_message``, ``_bump_last_message_at``,
``_templated_response``, ``_dispatch_reply``, ``_stamp_provider_message_id``)
via a late (function-body) import of the ``pipeline`` module itself — mirrors
pipeline.py's own existing late-import discipline for
chatwoot.*/secrets/demo_*/products.storage, applied one level up to dodge the
otherwise-circular ``pipeline.py -> pipeline_voice.py -> pipeline.py`` import.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from polaris_shared_types import IncomingMessage, PipelineDecision

from polaris_api.config import get_settings
from polaris_api.services import conversation as conversation_service
from polaris_api.services import media_download, transcription
from polaris_api.services.followups import scheduler as followup_scheduler

if TYPE_CHECKING:
    # Type-only import — avoids a hard circular import at module-load time.
    # pipeline.py imports THIS module at its own top level (see pipeline.py's
    # import-block comment), so a runtime `from polaris_api.services.pipeline
    # import PipelineDeps` here would try to import pipeline.py before its
    # own PipelineDeps class is defined. Safe because `from __future__ import
    # annotations` makes every annotation below a deferred string — nothing
    # needs PipelineDeps to be a real name at runtime, only at type-check time.
    from polaris_api.services.pipeline import PipelineDeps

logger = logging.getLogger(__name__)

_VOICE_TOKEN_KIND: Final[dict[str, str]] = {
    "whatsapp": "meta_waba_access_token",
    "telegram": "telegram_bot_token",
}


_VOICE_GATED_REPLY: Final[str] = (
    "Sorry, I can't listen to voice notes right now — please type your "
    "message and I'll help you straight away."
)


_VOICE_GATED_NOTE: Final[str] = "Voice Note is a Pro+ Feature"


_VOICE_FAILED_REPLY: Final[str] = (
    "Sorry, I couldn't quite make out that voice note — could you type your message instead?"
)


@dataclass(slots=True)
class _VoiceNoteResult:
    """Outcome of the inbound voice-note branch.

    Exactly one of these is meaningful:
      * ``decision`` set → the branch fully handled the message (gated-out
        Starter, or a download/transcription failure); ``run`` returns it
        unchanged (the customer reply was already dispatched).
      * ``transcript`` set → transcription succeeded; ``run`` substitutes it
        as ``user_text`` and continues the normal pipeline.
    """

    decision: PipelineDecision | None = None
    transcript: str | None = None
    detected_language: str | None = None


def _find_audio_attachment(incoming: IncomingMessage) -> Any | None:
    """Return the first audio attachment on the inbound message, or None."""
    for att in incoming.attachments:
        if att.kind == "audio":
            return att
    return None


def _filename_for_mime(mime_type: str) -> str:
    """Pick an upload filename whose extension matches the audio mime.

    Groq routes on the extension, so a wrong one (e.g. ``.bin``) can be
    rejected. Covers the common voice-note containers; defaults to ``.ogg``
    (WhatsApp / Telegram voice is OGG/Opus).
    """
    mapping = {
        "audio/ogg": "voice.ogg",
        "audio/oga": "voice.oga",
        "audio/mpeg": "voice.mp3",
        "audio/mp3": "voice.mp3",
        "audio/mp4": "voice.m4a",
        "audio/m4a": "voice.m4a",
        "audio/x-m4a": "voice.m4a",
        "audio/wav": "voice.wav",
        "audio/x-wav": "voice.wav",
        "audio/webm": "voice.webm",
    }
    return mapping.get((mime_type or "").split(";")[0].strip().lower(), "voice.ogg")


async def _handle_inbound_voice_note(
    *,
    incoming: IncomingMessage,
    channel: str,
    deps: PipelineDeps,
    t0: float,
    attachment: Any,
) -> _VoiceNoteResult:
    """Tier-gate → download → transcribe one inbound voice note.

    Returns a ``_VoiceNoteResult`` (see its docstring). Never raises for an
    expected failure — a bad transcript yields the graceful fallback reply,
    never a wrong confident answer.
    """
    settings = get_settings()
    plan_tier, token = await _resolve_voice_prereqs(deps, incoming.tenant_id, channel)

    # Pro+ gate (Starter excluded). No transcription, no download, no Groq call.
    if plan_tier not in settings.voice_note_tiers:
        logger.info(
            "pipeline.voice_note: tenant=%s tier=%s not Pro+ — gated",
            incoming.tenant_id,
            plan_tier,
        )
        decision = await _emit_voice_fallback(
            incoming=incoming,
            channel=channel,
            deps=deps,
            t0=t0,
            customer_text=_VOICE_GATED_REPLY,
            private_note_text=_VOICE_GATED_NOTE,
        )
        return _VoiceNoteResult(decision=decision)

    try:
        audio_bytes, mime = await media_download.fetch_audio_bytes(
            provider=incoming.provider,
            attachment=attachment,
            access_token=token,
            # Post-rehaul every channel arrives via the Chatwoot agent-bot webhook,
            # so the attachment is a Chatwoot data_url (not a Meta/Telegram handle).
            # chatwoot_account_id is set by chatwoot_router when it builds the
            # IncomingMessage — the canonical "came via Chatwoot" signal.
            via_chatwoot=incoming.chatwoot_account_id is not None,
        )
        result = await transcription.transcribe(
            audio_bytes,
            mime,
            filename=_filename_for_mime(mime),
        )
    except (media_download.MediaDownloadError, transcription.TranscriptionError) as exc:
        logger.warning(
            "pipeline.voice_note: transcription failed tenant=%s channel=%s err=%s",
            incoming.tenant_id,
            channel,
            type(exc).__name__,
        )
        decision = await _emit_voice_fallback(
            incoming=incoming,
            channel=channel,
            deps=deps,
            t0=t0,
            customer_text=_VOICE_FAILED_REPLY,
            private_note_text=None,
        )
        return _VoiceNoteResult(decision=decision)

    logger.info(
        "pipeline.voice_note: transcribed tenant=%s channel=%s lang=%s chars=%d",
        incoming.tenant_id,
        channel,
        result.language,
        len(result.text),
    )
    return _VoiceNoteResult(
        transcript=result.text,
        detected_language=result.language,
    )


async def _resolve_voice_prereqs(
    deps: PipelineDeps,
    tenant_id: UUID,
    channel: str,
) -> tuple[str, str | None]:
    """Read the tenant plan tier + decrypt the channel download token.

    One short RLS-scoped transaction (mirrors ``_resolve_secret_token``):
    the GUC scopes both the ``tenants`` read and the ``tenant_secrets``
    decrypt. Returns ``(plan_tier, token)`` — token is ``None`` for
    direct-url channels (IG/Messenger/Chatwoot) or when absent/undecryptable.
    Defaults to ``"starter"`` if the tenant row is missing (fail-safe gate).
    """
    from polaris_api.services.secrets import get as secrets_get

    token_kind = _VOICE_TOKEN_KIND.get(channel)
    plan_tier = "starter"
    token: str | None = None
    try:
        async with deps.anon_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )
            row = await conn.fetchrow(
                "SELECT plan_tier FROM tenants WHERE id = $1",
                tenant_id,
            )
            if row is not None and row["plan_tier"]:
                plan_tier = row["plan_tier"]
            if token_kind is not None:
                token_bytes = await secrets_get(conn, tenant_id, token_kind)
                if token_bytes is not None:
                    try:
                        token = token_bytes.decode("utf-8")
                    except Exception:  # noqa: BLE001 — undecryptable → no token
                        token = None
    except Exception:  # noqa: BLE001 — a read failure must not crash inbound
        logger.warning(
            "pipeline.voice_note: prereq read failed tenant=%s — defaulting to gated",
            tenant_id,
        )
    return plan_tier, token


async def _emit_voice_fallback(
    *,
    incoming: IncomingMessage,
    channel: str,
    deps: PipelineDeps,
    t0: float,
    customer_text: str,
    private_note_text: str | None,
) -> PipelineDecision:
    """Persist + dispatch a templated voice-note fallback reply.

    Mirrors ``_emit_handoff_for_empty_body``: ensure conversation, save a
    ``[voice note]`` user placeholder, persist the templated assistant reply,
    dispatch it, and (best-effort) post a Chatwoot private note. Used for both
    the Starter gate and the transcription-failure path.
    """
    from polaris_api.services import pipeline as _pipeline

    cw_account_id: int | None = None
    cw_conv_id: int | None = None
    async with deps.anon_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(incoming.tenant_id),
            )
            # Voice notes don't do branch-scoped lookups; ignore the branch.
            conversation_id, _ = await _pipeline._ensure_conversation(
                conn=conn,
                tenant_id=incoming.tenant_id,
                channel=channel,
                channel_user_id=incoming.sender.channel_user_id,
                channel_id=incoming.channel_id,
            )
            await followup_scheduler.cancel_pending_followup(
                conn,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
            )
            # Link the existing Chatwoot conversation so the private note has a
            # target (Chatwoot owns creation — ids ride in on the payload).
            cw_account_id, cw_conv_id = await _pipeline._link_cw_conversation(
                conn=conn,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
                incoming=incoming,
            )
            user_msg_id = await conversation_service.save_message(
                conn=conn,
                redis=deps.redis,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
                role="user",
                content="[voice note]",
            )
            assistant_msg_id = await _pipeline._persist_no_llm_message(
                conn=conn,
                redis=deps.redis,
                tenant_id=incoming.tenant_id,
                conversation_id=conversation_id,
                content=customer_text,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            await _pipeline._bump_last_message_at(conn, conversation_id, incoming.tenant_id)

    response = _pipeline._templated_response(customer_text)
    provider_message_id = await _pipeline._dispatch_reply(
        deps=deps,
        tenant_id=incoming.tenant_id,
        channel=channel,
        channel_user_id=incoming.sender.channel_user_id,
        response=response,
        channel_id=incoming.channel_id,
        assistant_msg_id=assistant_msg_id,
    )
    if provider_message_id is not None:
        await _pipeline._stamp_provider_message_id(
            deps=deps,
            tenant_id=incoming.tenant_id,
            assistant_msg_id=assistant_msg_id,
            provider_message_id=provider_message_id,
        )

    # Best-effort private note (gated case posts the "Pro+ Feature" note).
    if private_note_text and cw_account_id and cw_conv_id:
        try:
            from polaris_api.services.chatwoot.client import (
                get_chatwoot_client as _get_cw_client,
            )

            await _get_cw_client().post_private_note(
                account_id=cw_account_id,
                conversation_id=cw_conv_id,
                content=private_note_text,
            )
        except Exception:
            logger.warning(
                "pipeline.voice_note: fallback private note failed tenant=%s cw_conv=%s",
                incoming.tenant_id,
                cw_conv_id,
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
