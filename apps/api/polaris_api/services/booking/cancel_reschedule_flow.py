"""Cancel + reschedule booking-command handlers — extracted from
conversational.py (Batch E, task E2; see PRP plan
audit-batch-e-structural-splits).

``_do_cancel`` / ``_do_reschedule`` / ``_resolve_provider_service`` — the
existing-booking action handlers ``handle_booking_command`` dispatches to for
the "cancel" and "reschedule" intents. ``_resolve_provider_service`` is also
called by the new-booking flow (``picker_statemachine._do_new_booking``).

``branch_config``, ``link_flow``, ``ensure_binding_configured``,
``cancel_booking``, ``cancel_booking_reminders``, ``get_settings`` are
accessed via ``conversational.<name>`` (not imported directly), so the
existing test suite's monkeypatches on
``polaris_api.services.booking.conversational.<name>`` keep working — see the
E2 GOTCHA in the PRP plan. ``_resolve_provider_service`` itself is re-exported
from ``conversational.py`` for the same reason (tests patch
``conversational._resolve_provider_service`` directly).
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg

from polaris_api.services.booking import conversational
from polaris_api.services.booking.binding_token import BindingTokenError
from polaris_api.services.booking.cancel_reschedule import (
    BookingNotOwned,
    CustomerBooking,
)
from polaris_api.services.booking.client import EABookingError, public_booking_url
from polaris_api.services.booking.formatting import _format_slot, _short_booking_link

logger = logging.getLogger(__name__)


async def _do_cancel(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    customer_handle: str,
    target: CustomerBooking,
    tenant_timezone: str | None = None,
) -> conversational.BookingReply:
    """Cancel the soonest booking directly (EA + Polaris row)."""
    slot = _format_slot(target, tenant_timezone)
    try:
        await conversational.cancel_booking(
            conn,
            tenant_id=tenant_id,
            booking_id=target.booking_id,
            customer_handle=customer_handle,
        )
    except BookingNotOwned:
        # Race: row changed out from under us (already cancelled, etc.).
        logger.info(
            "booking_command cancel: booking no longer owned tenant=%s booking=%s",
            tenant_id,
            target.booking_id,
        )
        return conversational.BookingReply(
            content="I couldn't find an active booking to cancel. It may "
            "already be cancelled. Reply *RESCHEDULE* to book a new time.",
            kind="error",
        )
    except EABookingError:
        logger.warning(
            "booking_command cancel: EA failure tenant=%s booking=%s",
            tenant_id,
            target.booking_id,
        )
        return conversational.BookingReply(
            content="Sorry, I couldn't cancel your booking right now. Please "
            "try again shortly, or reply *HELP* to reach our team.",
            kind="error",
        )

    # Void any pending reminders so the worker doesn't ping a cancelled slot.
    # Best-effort: wrap in a SAVEPOINT (nested transaction) so a failure here
    # (e.g. a permission/constraint error) rolls back ONLY this sub-statement and
    # cannot poison the outer pipeline transaction — otherwise the cancel above
    # would itself be rolled back on commit while the EA appointment stays gone.
    try:
        async with conn.transaction():
            await conversational.cancel_booking_reminders(conn, booking_id=target.booking_id)
    except Exception:  # noqa: BLE001 — reminder cleanup is best-effort
        logger.warning(
            "booking_command cancel: reminder void failed tenant=%s booking=%s",
            tenant_id,
            target.booking_id,
        )

    return conversational.BookingReply(
        content=f"Done. Your booking for {slot} has been cancelled. "
        "Reply *RESCHEDULE* any time to book again.",
        kind="cancelled",
    )


async def _do_reschedule(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    target: CustomerBooking,
    tenant_timezone: str | None = None,
) -> conversational.BookingReply:
    """Release the current slot and hand the customer a fresh bound EA link.

    If we can't build a bound link (no branch EA config or binding secret
    unset), we fall back to the tenant's public booking page and leave the
    existing booking untouched, telling the customer to reply CANCEL if
    they want to drop it — never silently strand a booking.
    """
    settings = conversational.get_settings()
    slot = _format_slot(target, tenant_timezone)

    slug = await conn.fetchval("SELECT slug FROM tenants WHERE id = $1", tenant_id)
    if not isinstance(slug, str) or not slug:
        logger.warning("booking_command reschedule: tenant has no slug tenant=%s", tenant_id)
        return conversational.BookingReply(
            content="Sorry, I couldn't start a reschedule right now. Reply "
            "*HELP* to reach our team.",
            kind="error",
        )

    try:
        base_url = public_booking_url(base_url=settings.ea_booking_public_url, tenant_slug=slug)
    except ValueError:
        logger.warning("booking_command reschedule: invalid base_url/slug tenant=%s", tenant_id)
        return conversational.BookingReply(
            content="Sorry, I couldn't start a reschedule right now. Reply "
            "*HELP* to reach our team.",
            kind="error",
        )

    # Resolve provider/service the same way new bookings do: branch config
    # first, then EA-API first-item fallback. Calling get_branch_ea_config
    # directly here meant tenants with no branch-level EA config (i.e. those
    # relying on EA defaults) always hit the unbound bare-link fallback below
    # and never got a real reschedule link.
    provider_id, service_id = await conversational._resolve_provider_service(
        tenant_id=tenant_id, conversation_id=conversation_id
    )

    # Without a provider+service or a usable binding secret we can't mint a
    # bound link. Fall back to the public page; keep the current booking.
    binding_ok = True
    try:
        conversational.ensure_binding_configured()
    except BindingTokenError:
        binding_ok = False

    if provider_id is None or service_id is None or not binding_ok:
        logger.info(
            "booking_command reschedule: unbound fallback tenant=%s "
            "provider=%s service=%s binding_ok=%s",
            tenant_id,
            provider_id,
            service_id,
            binding_ok,
        )
        return conversational.BookingReply(
            content=(
                f"*Reschedule here:*\n{base_url}\n\n"
                "Your current booking stays until you choose a new slot. "
                "Reply *CANCEL* if you'd like to cancel it instead."
            ),
            kind="reschedule_page",
        )

    # Bound link path: mint a pending booking + signed token tied to the
    # verified handle, then release the current slot.
    try:
        pending = await conversational.link_flow.create_pending_booking(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            service_name=target.service_name,
            ea_service_id=service_id,
            ea_provider_id=provider_id,
        )
    except asyncpg.PostgresError:
        logger.exception(
            "booking_command reschedule: pending booking insert failed tenant=%s",
            tenant_id,
        )
        return conversational.BookingReply(
            content=(
                f"*Reschedule here:*\n{base_url}\n\n"
                "Your current booking stays until you choose a new slot. "
                "Reply *CANCEL* if you'd like to cancel it instead."
            ),
            kind="reschedule_page",
        )

    short_url = _short_booking_link(pending.jti)

    # Release the current slot now (cancel old EA appt + void its reminders).
    # Best-effort: if the release fails the customer still gets a working
    # link; we just note the old slot may linger.
    released = False
    try:
        await conversational.cancel_booking(
            conn,
            tenant_id=tenant_id,
            booking_id=target.booking_id,
            customer_handle=customer_handle,
        )
        released = True
        try:
            async with conn.transaction():  # savepoint — see _do_cancel note
                await conversational.cancel_booking_reminders(conn, booking_id=target.booking_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "booking_command reschedule: reminder void failed tenant=%s booking=%s",
                tenant_id,
                target.booking_id,
            )
    except (BookingNotOwned, EABookingError, asyncpg.PostgresError):
        # Best-effort by contract: a DB error on the Polaris-row UPDATE (after the
        # EA cancel may already have succeeded) must NOT strand the customer — we
        # still hand them a working reschedule link below.
        logger.warning(
            "booking_command reschedule: could not release old slot tenant=%s booking=%s",
            tenant_id,
            target.booking_id,
            exc_info=True,
        )

    if released:
        body = (
            f"I've released your booking for {slot}. \n"
            f"*Reschedule here:*\n{short_url}\n"
            "_This link is valid for 1 hour._"
        )
    else:
        body = (
            f"To reschedule {slot}:\n"
            f"*Reschedule here:*\n{short_url}\n"
            "_This link is valid for 1 hour._\n\n"
            "Reply *CANCEL* if you'd also like to cancel your current booking."
        )
    return conversational.BookingReply(content=body, kind="reschedule_link")


async def _resolve_provider_service(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
) -> tuple[int | None, int | None]:
    """Resolve ``(provider_id, service_id)`` for a new booking.

    Resolution order (per GOTCHA #1 in the plan):
      (a) ``conversational.branch_config.get_branch_ea_config`` — branch-level defaults.
      (b) If either is None, fall back to the first service/provider from
          the EA API (``client.list_services`` / ``list_providers``).
      (c) Return ``(None, None)`` only if the EA API also fails or is empty.
    """
    from polaris_api.services.booking.client import get_ea_booking_client

    provider_id, service_id = await conversational.branch_config.get_branch_ea_config(
        tenant_id=tenant_id, conversation_id=conversation_id
    )

    if provider_id is not None and service_id is not None:
        return provider_id, service_id

    # Fall back to EA API first-item defaults.
    try:
        client = get_ea_booking_client()
        if service_id is None:
            services = await client.list_services(tenant_id=tenant_id)
            if services:
                service_id = int(services[0]["id"])
        if provider_id is None:
            providers = await client.list_providers(tenant_id=tenant_id)
            if providers:
                provider_id = int(providers[0]["id"])
    except Exception:  # noqa: BLE001 — fail-soft; caller falls back to book_page
        logger.warning(
            "booking_command new: EA service/provider list failed tenant=%s",
            tenant_id,
            exc_info=True,
        )

    return provider_id, service_id
