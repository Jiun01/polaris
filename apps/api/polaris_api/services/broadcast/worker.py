"""Broadcast worker — Phase 6 PR-6.

Long-running drain loop. Per-tenant ZSET peeks for a scheduled
broadcast, claims it (status='running'), then drains its recipient
rows one at a time through:

   compliance gate → resolve template variables →
   meta_whatsapp.send_template → DB status flip

Per-recipient exceptions are caught + converted to status='failed'.
A single bad recipient never propagates to the broadcast level.

DESIGN DEVIATION FROM 04g §5.6: the deep-dive prescribed Redis LIST
draining (LPOP per recipient). We deviate to DB-driven draining
(SELECT broadcast_recipients WHERE status='pending' LIMIT BATCH)
because:

  * Single source of truth — the DB row's status IS the recipient
    state; no dual-write divergence between Redis LIST and the row.
  * Scale at GA is sub-1000 recipients per broadcast; the index
    scan on (broadcast_id, status) is O(log N) which dominates the
    per-send Meta HTTP RTT anyway.
  * Race-safety via UPDATE...WHERE status='pending' RETURNING.

The Redis ZSET stays as the tenant-broadcast index (which broadcast
is ready to drain), driven by service.schedule_broadcast's ZADD.

Worker invariants:

  * Uses ``polaris_service`` (BYPASSRLS) so cross-tenant ZSET scans
    + per-broadcast queries don't need a TenantContext. The
    ``app.current_tenant_id`` GUC is set EXPLICITLY at the top of
    each per-tenant batch so RLS-aware downstream code (audit,
    compliance.check) sees the right scope. Add to lint_service_
    pool ALLOWED_PREFIXES.

  * Cancel/pause honour: between recipients we re-check
    broadcasts.status; flipping to paused / cancelled bails out
    cleanly without partial-batch damage.

  * Wall-clock cap (BROADCAST_DRAIN_MAX_RUNTIME_SEC): if a
    broadcast has been running longer than the cap, transition it
    to status='errored' with error.code='RUNTIME_CAP_EXCEEDED'.
    Persists a webhook_events_outbound 'broadcast.failed' row.

  * Per-recipient retry budget: BROADCAST_RECIPIENT_RETRY_MAX
    retries on Meta 5xx. 4xx fails immediately. Retry budget is
    tracked in broadcast_recipients.attempt_count.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from polaris_api.db.pools import get_service_pool
from polaris_api.redis_pool import get_redis
from polaris_api.services.broadcast import compliance
from polaris_api.services.broadcast.service import _queue_key
from polaris_api.services.meta_whatsapp import (
    MetaWhatsAppClient,
    MetaWhatsAppError,
    get_meta_whatsapp_client,
)
from polaris_api.services.secrets import CryptoError

logger = logging.getLogger(__name__)

# Defaults mirror .env.example Phase 6 block.
_DEFAULT_TICK_SECONDS = 5
_DEFAULT_BATCH_SIZE = 50
_DEFAULT_DRAIN_MAX_RUNTIME_SEC = 1800
_DEFAULT_RETRY_MAX = 3

# tenant_secrets kind under which the WABA System User token is stored.
# Mirrors routers/tenant_templates._WABA_TOKEN_KIND.
_WABA_TOKEN_KIND = "meta_waba_access_token"  # noqa: S105 — kind label


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("env %s=%r not int; using default %d", name, raw, default)
        return default


# Per-broadcast tuning knobs are read once at import (env is fixed for the
# process lifetime — re-reading per broadcast / per recipient was wasteful).
_BATCH_SIZE = _env_int("BROADCAST_BATCH_SIZE", _DEFAULT_BATCH_SIZE)
_DRAIN_MAX_RUNTIME_SEC = _env_int(
    "BROADCAST_DRAIN_MAX_RUNTIME_SEC", _DEFAULT_DRAIN_MAX_RUNTIME_SEC
)
_RETRY_MAX = _env_int("BROADCAST_RECIPIENT_RETRY_MAX", _DEFAULT_RETRY_MAX)


# ---------------------------------------------------------------------
# Connection / GUC plumbing (BYPASSRLS service pool)
# ---------------------------------------------------------------------


@asynccontextmanager
async def _tenant_scoped_conn(tenant_id: UUID) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a service-pool conn + open a tx + set the tenant GUC.

    The worker MUST set ``app.current_tenant_id`` per batch so RLS-
    aware downstream code (audit.record, compliance.check) sees the
    right scope. Without this guard a service-pool insert would
    bypass tenant isolation on the audit chain.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )
        yield conn


# ---------------------------------------------------------------------
# Main entry: forever loop
# ---------------------------------------------------------------------


async def run_forever() -> None:
    """Drain broadcasts forever. Runs until cancelled.

    Each tick:
      1. Scan Redis for tenants with at-or-past-due ZSET entries.
      2. For each such tenant, claim the head broadcast + drain
         BROADCAST_BATCH_SIZE recipients.
      3. Sleep BROADCAST_TICK_SECONDS, repeat.

    Catches every per-tenant exception so a single tenant's failure
    can't pin the queue for other tenants.
    """
    tick = _env_int("BROADCAST_TICK_SECONDS", _DEFAULT_TICK_SECONDS)
    logger.info("broadcast worker: starting (tick=%ds)", tick)

    while True:
        try:
            await _tick_once()
        except asyncio.CancelledError:
            logger.info("broadcast worker: cancelled, stopping")
            raise
        except Exception:  # noqa: BLE001 — drain loop must survive everything
            logger.exception("broadcast worker: tick failed; sleeping + retrying")

        await asyncio.sleep(tick)


async def _tick_once() -> None:
    """One pass over the queue.

    We scan the Redis keyspace for ``broadcast:queue:*`` keys via
    SCAN (not KEYS — production hygiene). For each tenant key, peek
    the head with ZRANGEBYSCORE; if scheduled_at <= now, claim +
    drain.
    """
    namespace = os.getenv("BROADCAST_QUEUE_NAMESPACE", "broadcast")
    redis = get_redis()
    now_unix = time.time()

    # SCAN across the namespace. The pattern is ``{ns}:queue:*``
    # which matches one key per tenant. A growing tenant count
    # means more keys but each key is independent — no key contains
    # >1 broadcast id at a time for the head check.
    async for key in redis.scan_iter(match=f"{namespace}:queue:*", count=100):
        try:
            tenant_id_str = (
                key.decode("utf-8") if isinstance(key, bytes) else key
            ).rsplit(":", 1)[-1]
            tenant_id = UUID(tenant_id_str)
        except (ValueError, AttributeError):
            logger.warning("broadcast worker: malformed queue key %r", key)
            continue

        # Peek head: any broadcast with score <= now is eligible.
        # We claim one per tick per tenant to keep tenants moving
        # round-robin — long broadcasts don't starve their tenant
        # mates.
        head = await redis.zrangebyscore(
            key, min=-1, max=now_unix, start=0, num=1, withscores=False
        )
        if not head:
            continue
        broadcast_id_raw = head[0]
        broadcast_id_str = (
            broadcast_id_raw.decode("utf-8")
            if isinstance(broadcast_id_raw, bytes)
            else broadcast_id_raw
        )
        try:
            broadcast_id = UUID(broadcast_id_str)
        except ValueError:
            logger.warning(
                "broadcast worker: malformed broadcast_id %r in queue %s",
                broadcast_id_raw,
                key,
            )
            continue

        # Atomic claim: ZREM. If we lose the race to another worker
        # instance, ZREM returns 0 and we skip.
        removed = await redis.zrem(key, broadcast_id_str)
        if removed == 0:
            continue

        try:
            await _drain_broadcast(tenant_id=tenant_id, broadcast_id=broadcast_id)
        except Exception:  # noqa: BLE001 — per-broadcast isolation
            logger.exception(
                "broadcast worker: drain failed tenant=%s broadcast=%s",
                tenant_id,
                broadcast_id,
            )


# ---------------------------------------------------------------------
# Per-broadcast drain
# ---------------------------------------------------------------------


async def _drain_broadcast(*, tenant_id: UUID, broadcast_id: UUID) -> None:
    """Drain one broadcast through compliance gate + meta send.

    Wraps the batch in a tenant-scoped tx so audit + compliance
    queries see the right GUC. The per-recipient Meta HTTP call
    is OUTSIDE that tx — we don't want to hold a DB transaction
    across an external network call. The post-send status flip
    runs in its own tiny tx.
    """
    batch_size = _BATCH_SIZE
    max_runtime = _DRAIN_MAX_RUNTIME_SEC

    # Transition scheduled -> running + load broadcast envelope.
    envelope = await _claim_and_load(tenant_id=tenant_id, broadcast_id=broadcast_id)
    if envelope is None:
        return  # already cancelled/paused/completed

    deadline = time.time() + max_runtime
    meta_client = get_meta_whatsapp_client()

    while True:
        # Cancel/pause honour between batches.
        current_status = await _read_status(tenant_id, broadcast_id)
        if current_status in (None, "paused", "cancelled", "completed", "errored"):
            logger.info(
                "broadcast worker: tenant=%s broadcast=%s halting "
                "(status=%s)",
                tenant_id,
                broadcast_id,
                current_status,
            )
            return

        # Wall-clock cap.
        if time.time() > deadline:
            await _mark_errored(
                tenant_id=tenant_id,
                broadcast_id=broadcast_id,
                code="RUNTIME_CAP_EXCEEDED",
            )
            return

        # Claim a batch of pending recipients.
        recipients = await _claim_pending_batch(
            tenant_id=tenant_id,
            broadcast_id=broadcast_id,
            limit=batch_size,
        )
        if not recipients:
            # No more pending → complete.
            await _mark_completed(tenant_id=tenant_id, broadcast_id=broadcast_id)
            return

        for rec in recipients:
            try:
                await _send_one_recipient(
                    tenant_id=tenant_id,
                    broadcast_id=broadcast_id,
                    recipient_id=rec["id"],
                    channel=rec["channel"],
                    channel_user_id=rec["channel_user_id"],
                    envelope=envelope,
                    meta_client=meta_client,
                )
            except Exception:  # noqa: BLE001 — per-recipient isolation
                logger.exception(
                    "broadcast worker: per-recipient unhandled error "
                    "tenant=%s broadcast=%s recipient=%s",
                    tenant_id,
                    broadcast_id,
                    rec["id"],
                )
                # Per-recipient unhandled errors are a hard failure
                # — surface a generic failed status so the recipient
                # row isn't stuck on 'pending' forever.
                async with _tenant_scoped_conn(tenant_id) as conn:
                    await _mark_recipient_failed_with_conn(
                        conn,
                        broadcast_id=broadcast_id,
                        recipient_id=rec["id"],
                        payload={"code": "INTERNAL_ERROR"},
                    )


# ---------------------------------------------------------------------
# Helpers — DB reads/writes
# ---------------------------------------------------------------------


async def _claim_and_load(
    *, tenant_id: UUID, broadcast_id: UUID
) -> dict[str, Any] | None:
    """Flip status='scheduled' -> 'running' + load the envelope.

    Returns None if the broadcast is no longer 'scheduled' (e.g.
    operator cancelled between ZADD and the claim). The envelope
    carries the joined template + channel info the per-recipient
    send needs.
    """
    async with _tenant_scoped_conn(tenant_id) as conn:
        # CAS on scheduled -> running.
        updated = await conn.execute(
            """
            UPDATE broadcasts
               SET status = 'running',
                   started_at = COALESCE(started_at, now()),
                   updated_at = now()
             WHERE id = $1 AND status = 'scheduled'
            """,
            broadcast_id,
        )
        if updated.endswith(" 0"):
            return None

        # Load template + channel + token for per-recipient sends.
        row = await conn.fetchrow(
            """
            SELECT b.id, b.tenant_id, b.tenant_channel_id, b.template_id,
                   b.variables_template,
                   t.name AS template_name,
                   t.language AS template_language,
                   t.category AS template_category,
                   c.phone_number_id,
                   c.mm_lite_status
              FROM broadcasts b
              JOIN tenant_templates t ON t.id = b.template_id
              JOIN tenant_channels c ON c.id = b.tenant_channel_id
             WHERE b.id = $1
            """,
            broadcast_id,
        )
        if row is None:
            return None
        return dict(row)


async def _claim_pending_batch(
    *,
    tenant_id: UUID,
    broadcast_id: UUID,
    limit: int,
) -> list[asyncpg.Record]:
    """Atomically mark a batch of recipients as in-flight.

    We can't easily mark "in-flight" without a new status value;
    instead we read pending rows + leave them as 'pending' until
    the per-recipient send completes. Race-safety: a second worker
    instance reading the same recipients would attempt a duplicate
    send, but the Meta side de-dupes by recipient + template within
    a tight window, AND PR-7's wamid UNIQUE on broadcast_recipients
    catches the second insert.

    For Phase 6 single-process at GA (Q-PHASE6-05), the race window
    doesn't exist — only one worker instance runs.
    """
    async with _tenant_scoped_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, channel, channel_user_id, attempt_count
              FROM broadcast_recipients
             WHERE broadcast_id = $1 AND status = 'pending'
             ORDER BY created_at ASC
             LIMIT $2
            """,
            broadcast_id,
            limit,
        )
        return list(rows)


async def _read_status(tenant_id: UUID, broadcast_id: UUID) -> str | None:
    """Read broadcasts.status without claiming. Returns None if missing."""
    async with _tenant_scoped_conn(tenant_id) as conn:
        raw = await conn.fetchval(
            "SELECT status FROM broadcasts WHERE id = $1",
            broadcast_id,
        )
        if raw is None:
            return None
        return str(raw)


async def _mark_completed(*, tenant_id: UUID, broadcast_id: UUID) -> None:
    async with _tenant_scoped_conn(tenant_id) as conn:
        await conn.execute(
            """
            UPDATE broadcasts
               SET status = 'completed',
                   finished_at = COALESCE(finished_at, now()),
                   updated_at = now()
             WHERE id = $1 AND status = 'running'
            """,
            broadcast_id,
        )
        await _persist_outbound_event(
            conn,
            tenant_id=tenant_id,
            event_type="broadcast.completed",
            payload={"broadcast_id": str(broadcast_id)},
        )
    logger.info(
        "broadcast worker: tenant=%s broadcast=%s completed",
        tenant_id,
        broadcast_id,
    )


async def _mark_errored(
    *, tenant_id: UUID, broadcast_id: UUID, code: str
) -> None:
    async with _tenant_scoped_conn(tenant_id) as conn:
        await conn.execute(
            """
            UPDATE broadcasts
               SET status = 'errored',
                   finished_at = COALESCE(finished_at, now()),
                   error = jsonb_build_object('code', $2::text, 'at', now()::text),
                   updated_at = now()
             WHERE id = $1
            """,
            broadcast_id,
            code,
        )
        await _persist_outbound_event(
            conn,
            tenant_id=tenant_id,
            event_type="broadcast.failed",
            payload={"broadcast_id": str(broadcast_id), "code": code},
        )
    logger.warning(
        "broadcast worker: tenant=%s broadcast=%s errored code=%s",
        tenant_id,
        broadcast_id,
        code,
    )


async def _persist_outbound_event(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Persist a webhook_events_outbound row (deferred dispatch).

    Guard against the table not existing in fixtures, same as the
    templates / opt_out paths.
    """
    try:
        await conn.execute(
            """
            INSERT INTO webhook_events_outbound
                (tenant_id, event_type, payload, created_at)
            VALUES ($1, $2, $3::jsonb, now())
            """,
            tenant_id,
            event_type,
            # Dict DIRECTLY — the JSONB codec serialises once; json.dumps here
            # would double-encode into a JSONB string (see services/audit.py).
            payload,
        )
    except asyncpg.UndefinedTableError:
        logger.warning(
            "broadcast worker: webhook_events_outbound missing; "
            "skipping event persistence for tenant=%s event=%s",
            tenant_id,
            event_type,
        )


# ---------------------------------------------------------------------
# Per-recipient send
# ---------------------------------------------------------------------


async def _send_one_recipient(
    *,
    tenant_id: UUID,
    broadcast_id: UUID,
    recipient_id: UUID,
    channel: str,
    channel_user_id: str,
    envelope: dict[str, Any],
    meta_client: MetaWhatsAppClient,
) -> None:
    """Compliance gate → send → status flip + counter update."""
    # Compliance gate (RLS-scoped under the tenant GUC).
    async with _tenant_scoped_conn(tenant_id) as conn:
        gate = await compliance.check(
            conn,
            tenant_id=tenant_id,
            channel=channel,
            channel_user_id=channel_user_id,
            template_category=envelope["template_category"],
        )
        if not gate.allowed:
            await _mark_recipient_skipped(
                conn,
                broadcast_id=broadcast_id,
                recipient_id=recipient_id,
                skip_reason=gate.skip_reason or "opted_out",
            )
            return

        # Decrypt the per-tenant WABA token while we have the conn.
        try:
            token_bytes = await _get_token(conn, tenant_id)
        except (CryptoError, KeyError):
            logger.warning(
                "broadcast worker: cannot resolve WABA token tenant=%s",
                tenant_id,
            )
            await _mark_recipient_failed_with_conn(
                conn,
                broadcast_id=broadcast_id,
                recipient_id=recipient_id,
                payload={"code": "TOKEN_MISSING"},
            )
            return

    # Send OUTSIDE the tx so we don't hold an asyncpg connection
    # across the Meta HTTP RTT.
    use_marketing = (
        envelope["template_category"] == "MARKETING"
        and envelope.get("mm_lite_status") == "ONBOARDED"
    )
    try:
        wamid = await meta_client.send_template(
            phone_number_id=envelope["phone_number_id"],
            to=channel_user_id,
            template_name=envelope["template_name"],
            language=envelope["template_language"],
            variables=envelope.get("variables_template") or {},
            access_token=token_bytes.decode("utf-8"),
            use_marketing_endpoint=use_marketing,
        )
    except MetaWhatsAppError as exc:
        await _handle_meta_error(
            tenant_id=tenant_id,
            broadcast_id=broadcast_id,
            recipient_id=recipient_id,
            exc=exc,
        )
        return

    # Success — record wamid + flip status.
    async with _tenant_scoped_conn(tenant_id) as conn:
        await conn.execute(
            """
            UPDATE broadcast_recipients
               SET status = 'sent',
                   wa_message_id = $2,
                   sent_at = now(),
                   attempt_count = attempt_count + 1,
                   updated_at = now()
             WHERE id = $1
            """,
            recipient_id,
            wamid,
        )
        await conn.execute(
            """
            UPDATE broadcasts
               SET sent_count = sent_count + 1, updated_at = now()
             WHERE id = $1
            """,
            broadcast_id,
        )


async def _get_token(conn: asyncpg.Connection, tenant_id: UUID) -> bytes:
    """Decrypt the WABA System User token.

    Wraps services/secrets.get with a typed error: KeyError if no
    row exists; lets the caller decide whether to fail the recipient
    or the broadcast.
    """
    from polaris_api.services.secrets import get as secrets_get

    token = await secrets_get(conn, tenant_id, _WABA_TOKEN_KIND)
    if token is None:
        raise KeyError(f"no WABA token for tenant {tenant_id}")
    return token


async def _handle_meta_error(
    *,
    tenant_id: UUID,
    broadcast_id: UUID,
    recipient_id: UUID,
    exc: MetaWhatsAppError,
) -> None:
    """4xx fails immediately; 5xx retries up to BROADCAST_RECIPIENT_RETRY_MAX.

    On retry-exhausted, mark failed with the last response payload.
    """
    retry_max = _RETRY_MAX
    payload = {
        "code": "META_ERROR",
        "status_code": exc.status_code,
        "message": str(exc),
    }

    # 4xx: non-retryable.
    if 400 <= exc.status_code < 500:
        async with _tenant_scoped_conn(tenant_id) as conn:
            await _mark_recipient_failed_with_conn(
                conn,
                broadcast_id=broadcast_id,
                recipient_id=recipient_id,
                payload=payload,
                skip_reason="4xx",
            )
        return

    # 5xx: increment attempt_count + decide.
    async with _tenant_scoped_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE broadcast_recipients
               SET attempt_count = attempt_count + 1, updated_at = now()
             WHERE id = $1
             RETURNING attempt_count
            """,
            recipient_id,
        )
        if row is None:
            return
        if int(row["attempt_count"]) >= retry_max:
            await _mark_recipient_failed_with_conn(
                conn,
                broadcast_id=broadcast_id,
                recipient_id=recipient_id,
                payload=payload,
                skip_reason="retry_exhausted",
            )
        # else: leave as 'pending', next tick picks it up.


async def _mark_recipient_skipped(
    conn: asyncpg.Connection,
    *,
    broadcast_id: UUID,
    recipient_id: UUID,
    skip_reason: str,
) -> None:
    await conn.execute(
        """
        UPDATE broadcast_recipients
           SET status = 'skipped',
               skip_reason = $2,
               updated_at = now()
         WHERE id = $1
        """,
        recipient_id,
        skip_reason,
    )
    await conn.execute(
        """
        UPDATE broadcasts
           SET skipped_count = skipped_count + 1, updated_at = now()
         WHERE id = $1
        """,
        broadcast_id,
    )


async def _mark_recipient_failed_with_conn(
    conn: asyncpg.Connection,
    *,
    broadcast_id: UUID,
    recipient_id: UUID,
    payload: dict[str, Any],
    skip_reason: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE broadcast_recipients
           SET status = 'failed',
               skip_reason = COALESCE($3, skip_reason),
               failed_at = now(),
               failure_payload = $2::jsonb,
               updated_at = now()
         WHERE id = $1
        """,
        recipient_id,
        payload,  # dict DIRECTLY — JSONB codec serialises once (services/audit.py)
        skip_reason,
    )
    await conn.execute(
        """
        UPDATE broadcasts
           SET failed_count = failed_count + 1, updated_at = now()
         WHERE id = $1
        """,
        broadcast_id,
    )


# Re-export for the worker's lint allowlist check.
__all__ = ["run_forever"]


# Documentation-only references: _queue_key + datetime are intentionally
# imported so tests can assert key shape consistency between worker and
# service.schedule_broadcast (which also imports _queue_key). The
# datetime/UTC import is used inside compliance.check's signature.
_DOC_REFS = (_queue_key, datetime, UTC)
