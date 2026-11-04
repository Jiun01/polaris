"""Booking-via-EA-link lifecycle (v2.1 / PR-1).

Three operations on the Polaris side of the link flow:

  * ``create_pending_booking`` — the booking node mints a pending
    ``bookings`` row keyed on the VERIFIED ``customer_handle`` (the chat
    identity) with a binding ``jti`` + expiry, then hands back the row id
    and ``jti`` so the node can build the signed EA link.

  * ``confirm_booking`` — the EA inbound webhook resolves a pending row
    by its ``jti`` and flips it to ``confirmed`` with the EA
    ``appointment_id`` + slot. Idempotent on re-delivery.

  * ``sweep_expired_pending`` — a periodic task cancels pending rows
    whose binding token has expired (abandoned links).

All use the BYPASSRLS service pool (the EA webhook has no TenantContext;
the booking node runs inside the trusted inbound pipeline). Every query
is explicitly scoped by ``tenant_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from polaris_api.config import get_settings
from polaris_api.db.pools import get_service_pool

logger = logging.getLogger(__name__)

# Fallback when a tenant has no booking_config.reminder_offset_hours set.
# Matches BookingConfig.reminder_offset_hours default (routers/tenant_bookings).
_DEFAULT_REMINDER_OFFSET_HOURS = 24

# tenant_secrets KIND for the per-tenant WABA System User token (mirrors
# booking_reminders + tenant_templates).
_WABA_TOKEN_KIND = "meta_waba_access_token"  # noqa: S105 — secret KIND label


@dataclass(frozen=True)
class PendingBooking:
    """Returned by ``create_pending_booking`` — the node needs both to
    build the link (``jti`` goes into the signed token)."""

    booking_id: UUID
    jti: str


async def create_pending_booking(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    service_name: str | None = None,
    ttl_seconds: int | None = None,
    ea_service_id: int | None = None,
    ea_provider_id: int | None = None,
) -> PendingBooking:
    """Insert a ``pending`` booking bound to the verified customer handle.

    ``customer_handle`` MUST be the verified ``conversations.channel_user_id``
    (phone / IGSID / PSID) — never a value the customer typed. This is the
    anchor the cancel/reschedule path keys on.

    ``ea_service_id`` / ``ea_provider_id`` — the EA service + provider ids
    resolved at mint time. Stored so the ``/b/{jti}`` redirect can rebuild the
    EA wizard URL deterministically at click time without a live EA call.
    NULL when not provided (older rows from before migration 117).
    """
    settings = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.booking_binding_ttl_seconds
    jti = uuid4().hex
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO bookings
                (tenant_id, conversation_id, channel, customer_handle,
                 status, service_name, binding_token, binding_expires_at,
                 ea_service_id, ea_provider_id)
            VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7, $8, $9)
            RETURNING id
            """,
            tenant_id,
            conversation_id,
            channel,
            customer_handle,
            service_name,
            jti,
            expires_at,
            ea_service_id,
            ea_provider_id,
        )
    if row is None:  # pragma: no cover — INSERT ... RETURNING always returns
        raise RuntimeError("create_pending_booking: no row returned")
    return PendingBooking(booking_id=row["id"], jti=jti)


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of an EA webhook confirm attempt."""

    outcome: str  # "confirmed" | "already_confirmed" | "not_found"
    booking_id: UUID | None


async def confirm_booking(
    *,
    tenant_id: UUID,
    jti: str,
    appointment_id: str,
    start_at: datetime,
    end_at: datetime | None,
    service_name: str | None = None,
) -> ConfirmResult:
    """Flip a pending booking to confirmed. Idempotent.

    First delivery: matches the pending row by ``jti`` + ``tenant_id``,
    sets the EA appointment + slot, clears the binding token. Re-delivery
    (token already cleared): falls back to matching the now-confirmed row
    by ``(tenant_id, appointment_id)`` and reports ``already_confirmed``.
    """
    pool = get_service_pool()
    # The single UPDATE gated on status='pending' is already race-safe: a
    # concurrent re-delivery blocks on the row lock, then re-evaluates the
    # WHERE after the first commits, matches 0 rows, and falls through to
    # the idempotent already_confirmed path below.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE bookings
               SET appointment_id     = $3,
                   start_at           = $4,
                   end_at             = $5,
                   service_name       = COALESCE($6, service_name),
                   status             = 'confirmed',
                   binding_token      = NULL,
                   binding_expires_at = NULL,
                   updated_at         = now()
             WHERE tenant_id = $1
               AND binding_token = $2
               AND status = 'pending'
            RETURNING id, customer_handle, channel, service_name
            """,
            tenant_id,
            jti,
            appointment_id,
            start_at,
            end_at,
            service_name,
        )
        if row is not None:
            # Schedule the pre-appointment reminder now that the slot is
            # confirmed. Best-effort: a scheduling failure must NOT fail the
            # confirm (EA has already created the appointment) — the webhook
            # would otherwise 5xx and EA would retry a booking that is in fact
            # confirmed. This is the single choke point: initial bookings AND
            # conversational reschedules (a re-book that mints a fresh pending
            # row) both land here.
            await _schedule_reminder_for_confirmed(
                conn,
                tenant_id=tenant_id,
                booking_id=row["id"],
                start_at=start_at,
            )
            # Immediate "you're booked" confirmation. Best-effort, same
            # contract as the reminder scheduling above.
            await _send_confirmation_for_confirmed(
                conn,
                tenant_id=tenant_id,
                booking_id=row["id"],
                customer_handle=row.get("customer_handle"),
                channel=row.get("channel"),
                service_name=row.get("service_name"),
                start_at=start_at,
            )
            return ConfirmResult(outcome="confirmed", booking_id=row["id"])

        # Re-delivery / already consumed — look for the confirmed row by
        # appointment so we ack idempotently instead of 404-ing EA's retry.
        existing = await conn.fetchrow(
            """
            SELECT id FROM bookings
             WHERE tenant_id = $1 AND appointment_id = $2 AND status = 'confirmed'
             LIMIT 1
            """,
            tenant_id,
            appointment_id,
        )
    if existing is not None:
        return ConfirmResult(outcome="already_confirmed", booking_id=existing["id"])
    return ConfirmResult(outcome="not_found", booking_id=None)


async def _schedule_reminder_for_confirmed(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    booking_id: UUID,
    start_at: datetime,
) -> None:
    """Schedule the pre-appointment reminder for a freshly confirmed booking.

    Reads ``booking_config.reminder_offset_hours`` (default 24h) and inserts a
    ``booking_reminders`` row at ``start_at - offset``. ``schedule_reminder`` is
    idempotent (it updates an existing unsent reminder), so a re-confirm can't
    duplicate. Imports are local to avoid an import cycle with
    ``booking_reminders`` and to mirror the internal-E2E seeder's call shape.

    Best-effort by contract: every failure is swallowed + logged so the EA
    webhook still acks 200 for an appointment that is genuinely confirmed.
    """
    try:
        from polaris_api.services.booking.config import read_booking_config
        from polaris_api.services.booking_reminders import schedule_reminder

        booking_config = await read_booking_config(conn, tenant_id)
        offset_raw = booking_config.get(
            "reminder_offset_hours", _DEFAULT_REMINDER_OFFSET_HOURS
        )
        try:
            # float() first so a JSONB-encoded "48.0"/48.0 still maps to 48
            # rather than falling back to the default.
            offset_hours = int(float(offset_raw))
        except (TypeError, ValueError):
            offset_hours = _DEFAULT_REMINDER_OFFSET_HOURS

        await schedule_reminder(
            conn,
            booking_id=booking_id,
            tenant_id=tenant_id,
            start_at=start_at,
            offset_hours=offset_hours,
        )
    except Exception:  # noqa: BLE001 — reminder scheduling is best-effort
        logger.warning(
            "confirm_booking: reminder scheduling failed tenant=%s booking=%s",
            tenant_id,
            booking_id,
            exc_info=True,
        )


async def _send_confirmation_for_confirmed(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    booking_id: UUID,
    customer_handle: str | None,
    channel: str | None,
    service_name: str | None,
    start_at: datetime,
) -> None:
    """Send the immediate "you're booked" confirmation. Best-effort.

    Prefers the approved ``confirmation_template_name`` (delivers even outside
    WhatsApp's 24h window — the customer may have booked on the web without a
    recent inbound); falls back to the free-form ``confirmation_message``
    (in-window only). No-op when neither is configured, the channel isn't
    WhatsApp, or the WA channel / WABA token can't be resolved.

    Every failure is swallowed + logged: a confirmation that can't send must
    not fail the EA confirm (the appointment exists regardless).
    """
    try:
        from polaris_api.services.booking._notify import (
            format_slot,
            render_named,
            send_appointment_message,
            slot_date_time,
        )
        from polaris_api.services.booking.config import read_booking_config
        from polaris_api.services.meta_whatsapp import (
            MetaWhatsAppError,
            get_meta_whatsapp_client,
        )
        from polaris_api.services.secrets import get as secrets_get

        # Non-WhatsApp confirmation deferred (mirrors the reminder worker).
        if channel != "whatsapp" or not customer_handle:
            return

        booking_config = await read_booking_config(conn, tenant_id)
        template_name = booking_config.get("confirmation_template_name") or None
        confirmation_message = (booking_config.get("confirmation_message") or "").strip()
        if not template_name and not confirmation_message:
            return  # nothing configured to send

        chan = await conn.fetchrow(
            """
            SELECT phone_number_id FROM tenant_channels
             WHERE tenant_id = $1 AND channel = 'whatsapp' AND status = 'active'
             LIMIT 1
            """,
            tenant_id,
        )
        phone_number_id = chan["phone_number_id"] if chan else None
        if not phone_number_id:
            logger.warning(
                "confirm_booking: no active WA channel tenant=%s booking=%s",
                tenant_id,
                booking_id,
            )
            return

        token_bytes = await secrets_get(conn, tenant_id, _WABA_TOKEN_KIND)
        if token_bytes is None:
            logger.warning(
                "confirm_booking: no WABA token tenant=%s booking=%s",
                tenant_id,
                booking_id,
            )
            return
        access_token = token_bytes.decode("utf-8").strip()
        if not access_token:
            return

        tz = booking_config.get("tenant_timezone")
        service = service_name or "your appointment"
        slot = format_slot(start_at, tz)
        # Only render the free-form fallback when there's no template (the
        # template path ignores free_form_text). Keeps intent explicit.
        if template_name:
            free_form = ""
        else:
            date_str, time_str = slot_date_time(start_at, tz)
            free_form = render_named(
                confirmation_message,
                {"service": service, "date": date_str, "time": time_str},
            )

        try:
            await send_appointment_message(
                get_meta_whatsapp_client(),
                phone_number_id=str(phone_number_id),
                to=str(customer_handle),
                access_token=access_token,
                template_name=template_name,
                template_language=str(
                    booking_config.get("confirmation_template_language") or "en"
                ),
                template_vars={"1": service, "2": slot},
                free_form_text=free_form,
            )
        except MetaWhatsAppError as exc:
            # Outside the 24h window the free-form path returns 131026; the
            # template path avoids it. Either way: log, don't raise.
            logger.warning(
                "confirm_booking: confirmation send failed tenant=%s booking=%s status=%s",
                tenant_id,
                booking_id,
                exc.status_code,
            )
    except Exception:  # noqa: BLE001 — confirmation send is best-effort
        logger.warning(
            "confirm_booking: confirmation send errored tenant=%s booking=%s",
            tenant_id,
            booking_id,
            exc_info=True,
        )


async def sweep_expired_pending() -> int:
    """Cancel pending bookings whose binding token has expired.

    Returns the number of rows cancelled. Runs platform-wide (no tenant
    scope) via the service pool — intended for a periodic task.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE bookings
               SET status             = 'cancelled',
                   binding_token      = NULL,
                   binding_expires_at = NULL,
                   updated_at         = now()
             WHERE status = 'pending'
               AND binding_expires_at IS NOT NULL
               AND binding_expires_at < now()
            """,
        )
    # asyncpg execute() returns a tag like "UPDATE 3".
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):  # pragma: no cover — defensive
        return 0
