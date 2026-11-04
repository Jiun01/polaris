"""Follow-up worker tick — Phase 13 follow-ups PR-1.

Scans ``followup_jobs`` for due, unsent rows (service pool, BYPASSRLS,
cross-tenant) and, for each, RE-VERIFIES eligibility, generates a
context-aware re-engagement message, and sends it on the tenant's own
WABA inside the free 24h window. Mirrors ``booking_reminders.tick_due``.

Re-verification (``should_send_followup``) defends the cancel-on-inbound
race: a customer who replied after the job was scheduled has a newer
inbound than the job's ``anchor_inbound_at`` → skip. Also skips on
handoff, opt-out, disabled tenant, or a closed window.

Terminal vs retry: a 4xx Meta error (e.g. window closed) is terminal
(mark ``sent_at`` + ``last_error``, never retried); a 5xx / transient LLM
error increments ``attempts`` (capped at FOLLOWUP_MAX_ATTEMPTS).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

from polaris_api.services import conversation as conversation_service
from polaris_api.services import llm
from polaris_api.services import tenant_snapshot as repo_mod
from polaris_api.services._llm_models import LLMError
from polaris_api.services._llm_prompts import TenantPersona, coerce_persona_blocks
from polaris_api.services.followups.eligibility import (
    SendSignals,
    is_followup_enabled,
    should_send_followup,
)
from polaris_api.services.meta_whatsapp import MetaWhatsAppClient, MetaWhatsAppError

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 3
# Jobs drained per tick. A LIMIT here bounds the per-tick work; jobs beyond it
# are picked up on the next tick. Searchable + tunable here, not buried in SQL.
_TICK_BATCH_SIZE = 100
_WABA_TOKEN_KIND = "meta_waba_access_token"  # noqa: S105 — secret kind label


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def tick_due(conn: asyncpg.Connection, *, meta_client: MetaWhatsAppClient) -> int:
    """Process all due, unsent follow-ups. Returns the count sent."""
    max_attempts = _env_int("FOLLOWUP_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
    now_utc = datetime.now(UTC)

    rows = await conn.fetch(
        """
        SELECT
            j.id              AS job_id,
            j.tenant_id,
            j.conversation_id,
            j.anchor_inbound_at,
            j.attempts,
            c.channel,
            c.channel_user_id,
            c.handoff_at,
            t.settings        AS tenant_settings,
            tc.phone_number_id,
            (SELECT max(m.created_at) FROM messages m
              WHERE m.conversation_id = c.id AND m.role = 'user') AS latest_inbound_at,
            (SELECT am.opted_out_at FROM audience_members am
              WHERE am.tenant_id = j.tenant_id
                AND am.phone_number_e164 = c.channel_user_id) AS opted_out_at
          FROM followup_jobs j
          JOIN conversations c ON c.id = j.conversation_id
          JOIN tenants t       ON t.id = j.tenant_id
          LEFT JOIN tenant_channels tc
                 ON tc.tenant_id = j.tenant_id
                AND tc.channel = c.channel
                AND tc.status = 'active'
         WHERE j.sent_at IS NULL
           AND j.scheduled_at <= $1
           AND j.attempts < $2
         ORDER BY j.scheduled_at ASC
         LIMIT $3
        """,
        now_utc,
        max_attempts,
        _TICK_BATCH_SIZE,
    )

    sent_count = 0
    for row in rows:
        job_id = UUID(str(row["job_id"]))
        try:
            if await _process_one(
                conn,
                row=row,
                meta_client=meta_client,
                max_attempts=max_attempts,
                now_utc=now_utc,
            ):
                sent_count += 1
        except Exception:  # noqa: BLE001 — one bad job must not stall the tick
            logger.exception("followups: unhandled error job=%s", job_id)
            await _record_attempt_error(conn, job_id, "INTERNAL_ERROR")
    return sent_count


async def _process_one(
    conn: asyncpg.Connection,
    *,
    row: asyncpg.Record,
    meta_client: MetaWhatsAppClient,
    max_attempts: int,
    now_utc: datetime,
) -> bool:
    job_id = UUID(str(row["job_id"]))
    tenant_id = UUID(str(row["tenant_id"]))
    conversation_id = UUID(str(row["conversation_id"]))

    signals = SendSignals(
        enabled=is_followup_enabled(row["tenant_settings"]),
        channel=str(row["channel"]),
        handoff_at=row["handoff_at"],
        latest_inbound_at=row["latest_inbound_at"],
        anchor_inbound_at=row["anchor_inbound_at"],
        opted_out=row["opted_out_at"] is not None,
        now=now_utc,
        attempts=int(row["attempts"]),
        max_attempts=max_attempts,
    )
    if not should_send_followup(signals):
        # No longer valid (superseded / handoff / window closed / opted out /
        # disabled). Terminal — mark done so the worker stops scanning it.
        await _mark_terminal(conn, job_id, reason="ineligible_at_send")
        return False

    phone_number_id = row["phone_number_id"]
    if not phone_number_id:
        await _mark_terminal(conn, job_id, reason="no_phone_number_id")
        return False

    from polaris_api.services.secrets import get as secrets_get

    token = await secrets_get(conn, tenant_id, _WABA_TOKEN_KIND)
    if token is None:
        await _record_attempt_error(conn, job_id, "NO_WABA_TOKEN")
        return False
    decoded_token = token.decode("utf-8").strip()
    if not decoded_token:
        await _mark_terminal(conn, job_id, reason="empty_waba_token")
        return False

    # Build the context-aware message from the conversation history.
    snapshot = await repo_mod.load_tenant_snapshot(conn, tenant_id)
    persona = TenantPersona(
        assistant_name=snapshot.get("persona_assistant_name"),
        company_name=snapshot.get("persona_company_name"),
        system_prompt_override=snapshot.get("system_prompt"),
        persona_blocks=coerce_persona_blocks(snapshot.get("persona_blocks")),
    )
    window = await conversation_service.load_history(
        conn=conn,
        redis=None,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
    )
    history = [{"role": m.role, "content": m.content} for m in window.messages]
    try:
        text = await llm.generate_followup(
            conversation_history=history, persona=persona
        )
    except LLMError as exc:
        await _record_attempt_error(conn, job_id, f"LLM_{type(exc).__name__}")
        return False
    if not text:
        await _mark_terminal(conn, job_id, reason="empty_generation")
        return False

    try:
        await meta_client.send_text(
            phone_number_id=str(phone_number_id),
            to=str(row["channel_user_id"]),
            body=text,
            access_token=decoded_token,
        )
    except MetaWhatsAppError as exc:
        # 4xx (e.g. window closed / bad token) won't recover → terminal.
        # 5xx → retry next tick (bounded by max_attempts).
        if 400 <= exc.status_code < 500:
            await _mark_terminal(conn, job_id, reason=f"meta_{exc.status_code}")
        else:
            await _record_attempt_error(conn, job_id, f"META_{exc.status_code}")
        return False

    await _mark_sent(conn, job_id)
    logger.info("followups: sent job=%s tenant=%s conv=%s", job_id, tenant_id, conversation_id)
    return True


async def _mark_sent(conn: asyncpg.Connection, job_id: UUID) -> None:
    await conn.execute(
        "UPDATE followup_jobs SET sent_at = now(), updated_at = now() WHERE id = $1",
        job_id,
    )


async def _mark_terminal(conn: asyncpg.Connection, job_id: UUID, *, reason: str) -> None:
    """Terminal non-send: set sent_at (so the worker skips it) + record why."""
    await conn.execute(
        """
        UPDATE followup_jobs
           SET sent_at = now(), last_error = $2, updated_at = now()
         WHERE id = $1 AND sent_at IS NULL
        """,
        job_id,
        f"skipped:{reason}",
    )


async def _record_attempt_error(
    conn: asyncpg.Connection, job_id: UUID, error: str
) -> None:
    await conn.execute(
        """
        UPDATE followup_jobs
           SET attempts = attempts + 1, last_error = $2, updated_at = now()
         WHERE id = $1 AND sent_at IS NULL
        """,
        job_id,
        error,
    )


_DEFAULT_TICK_SECONDS = 60


async def run_forever() -> None:
    """Tick loop: drain due follow-ups every FOLLOWUP_TICK_SECONDS.

    Used both by the in-process lifespan task (main.py) and the standalone
    ``scripts/run_followup_worker.py``. Acquires the service pool per tick
    (BYPASSRLS cross-tenant scan), mirroring the reminder worker. Cancelled
    cleanly on shutdown.

    Concurrency posture (mirrors booking_reminders): single active worker
    assumed. The fetch has no ``FOR UPDATE SKIP LOCKED``, so running multiple
    instances would double-process a batch (the ``sent_at IS NULL`` guard
    prevents a double *mark*, but the Meta send happens before the mark → a
    duplicate message). Delivery is at-least-once: a crash between send and
    mark re-sends on the next tick. If horizontal scaling is introduced,
    switch the fetch to a ``SKIP LOCKED`` claim. Acceptable at GA (one
    in-process task per single API instance).
    """
    from polaris_api.db.pools import get_service_pool
    from polaris_api.services.meta_whatsapp import get_meta_whatsapp_client

    tick = _env_int("FOLLOWUP_TICK_SECONDS", _DEFAULT_TICK_SECONDS)
    meta_client = get_meta_whatsapp_client()
    logger.info("followup worker: starting (tick=%ds)", tick)

    while True:
        try:
            async with get_service_pool().acquire() as conn:
                sent = await tick_due(conn, meta_client=meta_client)
            if sent:
                logger.info("followup worker: sent %d follow-up(s)", sent)
        except asyncio.CancelledError:
            logger.info("followup worker: cancelled, stopping")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("followup worker: tick failed; sleeping + retrying")

        await asyncio.sleep(tick)
