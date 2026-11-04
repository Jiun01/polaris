"""Booking reminder service — Phase 13 PR-1.

Owns the lifecycle of booking_reminders rows:

  schedule_reminder(conn, booking_id, tenant_id, start_at, offset_hours)
    — Insert one reminder row at start_at - offset_hours. Idempotent:
      if a reminder already exists for the booking and is unsent, update
      its scheduled_at rather than inserting a duplicate.

  tick_due(conn, meta_client)
    — Find all due, unsent reminders; send each; mark sent. Runs inside
      the reminder worker (run_reminder_worker.py) every
      REMINDER_TICK_SECONDS seconds and also via the internal E2E
      endpoint /internal/e2e/booking-reminder-tick for test forcing.

  mark_sent(conn, reminder_id)
    — Idempotent: set sent_at = now() on the given row. No-op if
      already set. Used by tick_due after a successful send.

  cancel_booking_reminders(conn, booking_id)
    — Mark all unsent reminders for a booking as sent (with a sentinel
      sent_at timestamp) so the worker skips them. Called when a booking
      is cancelled or rescheduled before the reminder fires.

Quiet-hours logic:
  If tenants.settings.booking_config.quiet_hours_start + _end are both
  set, a due reminder whose scheduled_at (in UTC) lands in the quiet
  window is deferred: its scheduled_at is bumped to quiet_hours_end of
  the same or next day (UTC). The reminder row is updated in-place; the
  worker skips it this tick and picks it up on the next tick after the
  new scheduled_at.

Meta send:
  When the tenant has configured ``booking_config.reminder_template_name``
  the reminder is sent via an approved WhatsApp template (utility — the
  non-marketing endpoint), which delivers both inside AND outside the
  24-hour customer-initiated window. The template body takes two
  positional params: {{1}} = service, {{2}} = date/time.

  When no template is configured the reminder falls back to a free-form
  text message (send_text), which only delivers inside the 24h window;
  outside it Meta returns 131026 ("more than 24 hours have passed since
  the customer last replied"), which is caught, logged, and stored in
  booking_reminders.last_error without crashing the worker.

  Either way, when the customer replies to the reminder (CONFIRM /
  RESCHEDULE / CANCEL or free text) the inbound reopens the 24h window
  and the normal pipeline — including the conversational cancel/
  reschedule handler (services/booking/conversational.py) and human
  handoff — can respond freely.

Uses polaris_service pool (BYPASSRLS) — the worker does not set
app.current_tenant_id so it can scan across all tenants in one query.
Per-tenant GUC IS set around individual send+mark operations for
audit correctness per ADR-002.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

_DEFAULT_TICK_SECONDS = 60
_DEFAULT_MAX_ATTEMPTS = 3
_WABA_TOKEN_KIND = "meta_waba_access_token"  # noqa: S105


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("env %s=%r not int; using default %d", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# schedule_reminder
# ---------------------------------------------------------------------------


async def schedule_reminder(
    conn: asyncpg.Connection,
    *,
    booking_id: UUID,
    tenant_id: UUID,
    start_at: datetime,
    offset_hours: int = 24,
) -> UUID:
    """Insert or update the unsent reminder for a booking.

    If an unsent reminder already exists for booking_id, update its
    scheduled_at (e.g. because the booking was rescheduled). Returns
    the reminder_id.
    """
    scheduled_at = start_at - timedelta(hours=offset_hours)

    existing = await conn.fetchrow(
        """
        SELECT id FROM booking_reminders
         WHERE booking_id = $1 AND sent_at IS NULL
         LIMIT 1
        """,
        booking_id,
    )
    if existing is not None:
        await conn.execute(
            """
            UPDATE booking_reminders
               SET scheduled_at = $2, updated_at = now()
             WHERE id = $1
            """,
            existing["id"],
            scheduled_at,
        )
        return UUID(str(existing["id"]))

    reminder_id = await conn.fetchval(
        """
        INSERT INTO booking_reminders (booking_id, tenant_id, scheduled_at)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        booking_id,
        tenant_id,
        scheduled_at,
    )
    return UUID(str(reminder_id))


# ---------------------------------------------------------------------------
# cancel_booking_reminders
# ---------------------------------------------------------------------------


async def cancel_booking_reminders(
    conn: asyncpg.Connection,
    *,
    booking_id: UUID,
) -> int:
    """Void all unsent reminders for a booking (cancel / reschedule path).

    Returns the count of rows voided.
    """
    result = await conn.execute(
        """
        UPDATE booking_reminders
           SET sent_at = now(),
               last_error = 'voided:booking_cancelled_or_rescheduled',
               updated_at = now()
         WHERE booking_id = $1 AND sent_at IS NULL
        """,
        booking_id,
    )
    count = int(result.split()[-1]) if result else 0
    if count:
        logger.info(
            "booking_reminders: voided %d reminder(s) for booking=%s",
            count, booking_id,
        )
    return count


# ---------------------------------------------------------------------------
# mark_sent
# ---------------------------------------------------------------------------


async def mark_sent(conn: asyncpg.Connection, *, reminder_id: UUID) -> None:
    """Idempotently mark a reminder row as sent."""
    await conn.execute(
        """
        UPDATE booking_reminders
           SET sent_at = COALESCE(sent_at, now()),
               updated_at = now()
         WHERE id = $1
        """,
        reminder_id,
    )


# ---------------------------------------------------------------------------
# tick_due — main worker entry
# ---------------------------------------------------------------------------


async def tick_due(
    conn: asyncpg.Connection,
    *,
    meta_client: object,  # MetaWhatsAppClient — avoid circular import at module level
) -> int:
    """Process all due, unsent reminders.

    Fetches due rows in a single query (polaris_service bypasses RLS so
    we get cross-tenant rows). For each row:
      1. Load the booking + tenant config (offset, quiet hours, channel).
      2. Apply quiet-hours deferral if configured — update scheduled_at,
         skip this tick.
      3. Resolve the per-tenant WABA token via secrets.get.
      4. Send via meta_client.send_text (WA) or log a skip for IG/Messenger
         (Phase 13 v2.1 — non-WA channels deferred).
      5. mark_sent on success; increment attempts + store last_error on failure.

    Returns the count of reminders that were sent (not deferred/failed).
    """
    max_attempts = _env_int("REMINDER_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
    now_utc = datetime.now(UTC)

    rows = await conn.fetch(
        """
        SELECT
          br.id            AS reminder_id,
          br.booking_id,
          br.tenant_id,
          br.scheduled_at,
          br.attempts,
          b.customer_handle,
          b.channel,
          b.start_at,
          b.service_name,
          b.status         AS booking_status,
          tc.phone_number_id,
          t.settings       AS tenant_settings
        FROM booking_reminders br
        JOIN bookings b          ON b.id  = br.booking_id
        JOIN tenants t           ON t.id  = br.tenant_id
        LEFT JOIN tenant_channels tc
               ON tc.tenant_id = br.tenant_id
              AND tc.channel = 'whatsapp'
              AND tc.status = 'active'
         WHERE br.sent_at  IS NULL
           AND br.scheduled_at <= $1
           AND br.attempts < $2
           AND b.status = 'confirmed'
         ORDER BY br.scheduled_at ASC
         LIMIT 100
        """,
        now_utc,
        max_attempts,
    )

    sent_count = 0

    for row in rows:
        reminder_id = UUID(str(row["reminder_id"]))
        tenant_id = UUID(str(row["tenant_id"]))

        try:
            sent = await _process_one(
                conn,
                row=row,
                reminder_id=reminder_id,
                tenant_id=tenant_id,
                now_utc=now_utc,
                meta_client=meta_client,
            )
            if sent:
                sent_count += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "booking_reminders: unhandled error reminder=%s booking=%s tenant=%s",
                reminder_id,
                row["booking_id"],
                tenant_id,
            )
            await _record_attempt_error(
                conn,
                reminder_id=reminder_id,
                error="INTERNAL_ERROR",
            )

    return sent_count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _process_one(
    conn: asyncpg.Connection,
    *,
    row: asyncpg.Record,
    reminder_id: UUID,
    tenant_id: UUID,
    now_utc: datetime,
    meta_client: object,
) -> bool:
    """Process a single due reminder. Returns True if sent, False if deferred."""
    # Quiet-hours deferral.
    settings_raw = row["tenant_settings"]
    if isinstance(settings_raw, str):
        try:
            settings_raw = json.loads(settings_raw)
        except json.JSONDecodeError:
            settings_raw = {}
    booking_config = (
        settings_raw.get("booking_config") or {}
        if isinstance(settings_raw, dict)
        else {}
    )
    qs_str = booking_config.get("quiet_hours_start")
    qe_str = booking_config.get("quiet_hours_end")

    if qs_str and qe_str:
        deferred_at = _apply_quiet_hours(now_utc, qs_str, qe_str)
        if deferred_at is not None:
            await conn.execute(
                """
                UPDATE booking_reminders
                   SET scheduled_at = $2, updated_at = now()
                 WHERE id = $1
                """,
                reminder_id,
                deferred_at,
            )
            logger.info(
                "booking_reminders: deferred reminder=%s to %s (quiet hours %s-%s)",
                reminder_id,
                deferred_at.isoformat(),
                qs_str,
                qe_str,
            )
            return False

    channel = str(row["channel"])
    if channel != "whatsapp":
        # Non-WA channels deferred to Phase 13 v2.1.
        logger.info(
            "booking_reminders: skipping channel=%s reminder=%s (Phase 13 v2.1)",
            channel,
            reminder_id,
        )
        await _record_attempt_error(
            conn, reminder_id=reminder_id, error="CHANNEL_NOT_SUPPORTED"
        )
        return False

    phone_number_id = row["phone_number_id"]
    if not phone_number_id:
        logger.warning(
            "booking_reminders: no active WA channel for tenant=%s reminder=%s",
            tenant_id,
            reminder_id,
        )
        await _record_attempt_error(
            conn, reminder_id=reminder_id, error="NO_WA_CHANNEL"
        )
        return False

    # Resolve WABA token.
    from polaris_api.services.secrets import get as secrets_get  # local import

    token_bytes = await secrets_get(conn, tenant_id, _WABA_TOKEN_KIND)
    if token_bytes is None:
        logger.warning(
            "booking_reminders: no WABA token tenant=%s reminder=%s",
            tenant_id,
            reminder_id,
        )
        await _record_attempt_error(
            conn, reminder_id=reminder_id, error="NO_WABA_TOKEN"
        )
        return False

    # Send.
    from polaris_api.services.meta_whatsapp import (  # local import
        MetaWhatsAppClient,
        MetaWhatsAppError,
    )
    assert isinstance(meta_client, MetaWhatsAppClient)

    # 24h-window handling: a reminder for a booking made >24h ago falls
    # outside WhatsApp's customer-initiated window, where only an approved
    # template will deliver. When the tenant has configured a reminder
    # template (booking_config.reminder_template_name) we send via the
    # template path (utility — non-marketing endpoint); otherwise we fall
    # back to free-form text, which only delivers inside the 24h window
    # (Meta 131026 otherwise, caught below). The template body is expected
    # to take two positional params: {{1}} = service, {{2}} = date/time.
    #
    # When the customer replies to the template (e.g. taps/sends CONFIRM /
    # RESCHEDULE / CANCEL) that inbound reopens the 24h window, so the
    # normal inbound pipeline — including the conversational cancel/
    # reschedule handler and human handoff — can reply freely.
    template_name = booking_config.get("reminder_template_name")
    template_language = booking_config.get("reminder_template_language") or "en"
    tenant_timezone = booking_config.get("tenant_timezone")
    use_template = (
        template_name if isinstance(template_name, str) and template_name else None
    )

    from polaris_api.services.booking._notify import send_appointment_message

    try:
        await send_appointment_message(
            meta_client,
            phone_number_id=str(phone_number_id),
            to=str(row["customer_handle"]),
            access_token=token_bytes.decode("utf-8").strip(),
            template_name=use_template,
            template_language=str(template_language),
            template_vars=_reminder_template_vars(row, tenant_timezone),
            free_form_text=_build_reminder_text(row, tenant_timezone),
        )
    except MetaWhatsAppError as exc:
        logger.warning(
            "booking_reminders: Meta error reminder=%s status=%d msg=%s",
            reminder_id,
            exc.status_code,
            exc,
        )
        await _record_attempt_error(
            conn,
            reminder_id=reminder_id,
            error=f"META_{exc.status_code}:{exc}",
        )
        return False

    await mark_sent(conn, reminder_id=reminder_id)
    logger.info(
        "booking_reminders: sent reminder=%s booking=%s to=%s",
        reminder_id,
        row["booking_id"],
        row["customer_handle"],
    )
    return True


def _reminder_template_vars(
    row: asyncpg.Record, tenant_timezone: str | None
) -> dict[str, str]:
    """Positional template variables for the reminder template.

    ``{"1": service, "2": slot}`` — mirrors the free-form text so a tenant's
    approved template (body params {{1}}, {{2}}) renders the same information.
    Whitelabel-clean; the slot is formatted in the tenant's timezone (UTC
    fallback) via the shared ``_notify.format_slot``.
    """
    from polaris_api.services.booking._notify import format_slot

    service = row["service_name"] or "your appointment"
    return {"1": service, "2": format_slot(row["start_at"], tenant_timezone)}


def _build_reminder_text(row: asyncpg.Record, tenant_timezone: str | None) -> str:
    """Build the customer-facing reminder message body.

    Whitelabel-clean: no vendor names. Appointment date is formatted in the
    tenant's timezone (UTC fallback) via the shared ``_notify.format_slot``.
    """
    from polaris_api.services.booking._notify import format_slot

    service = row["service_name"] or "your appointment"
    slot = format_slot(row["start_at"], tenant_timezone)
    return (
        f"Hi! Just a reminder that {service} is scheduled for {slot}.\n\n"
        "Reply *CONFIRM* to confirm, *RESCHEDULE* to pick a new time, "
        "or *CANCEL* to cancel."
    )


def _apply_quiet_hours(
    now_utc: datetime,
    start_str: str,
    end_str: str,
) -> datetime | None:
    """Return the time to defer to if now_utc is inside the quiet window.

    Returns None if now_utc is OUTSIDE the window (no deferral needed).

    Quiet window logic:
      * Parse start + end as HH:MM UTC times.
      * If start < end: window is same day (e.g. 01:00–06:00).
      * If start > end: window wraps midnight (e.g. 22:00–08:00 next day).
      * If now is inside: return today's end time (or tomorrow's if already
        past end on a wraparound).
    """
    try:
        qs = dt_time(int(start_str[:2]), int(start_str[3:5]))
        qe = dt_time(int(end_str[:2]), int(end_str[3:5]))
    except (ValueError, IndexError):
        return None

    now_time = now_utc.time().replace(second=0, microsecond=0)

    if qs < qe:
        # Normal window: e.g. 01:00–06:00
        in_window = qs <= now_time < qe
    else:
        # Wraparound: e.g. 22:00–08:00
        in_window = now_time >= qs or now_time < qe

    if not in_window:
        return None

    # Defer to end of quiet window.
    today = now_utc.date()
    candidate = datetime.combine(today, qe, tzinfo=UTC)
    if candidate <= now_utc:
        candidate += timedelta(days=1)
    return candidate


async def _record_attempt_error(
    conn: asyncpg.Connection,
    *,
    reminder_id: UUID,
    error: str,
) -> None:
    await conn.execute(
        """
        UPDATE booking_reminders
           SET attempts   = attempts + 1,
               last_error = $2,
               updated_at = now()
         WHERE id = $1
        """,
        reminder_id,
        error,
    )


__all__ = [
    "cancel_booking_reminders",
    "mark_sent",
    "schedule_reminder",
    "tick_due",
]
