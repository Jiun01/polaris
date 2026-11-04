"""Secure cancel / reschedule keyed on the verified chat identity (v2.1).

The impersonation defence lives here. A customer messaging "cancel my
booking" is identified ONLY by the verified ``channel_user_id`` from the
live signed webhook — never by anything they type. Every lookup and
mutation is gated on ``customer_handle = <verified handle>`` so Customer
A can never reach Customer B's booking: there is no typed-identity input
to spoof, and the EA call is only made AFTER an ownership-matched row is
found.

Functions take an ``asyncpg.Connection`` so the caller controls the pool
+ RLS posture (portal endpoint → request conn under RLS; pipeline/flow →
service pool). The ``tenant_id`` + ``customer_handle`` filters hold
regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from polaris_api.services.booking.client import (
    EABookingStructuralError,
    EABookingTransientError,
    get_ea_booking_client,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CustomerBooking:
    """A confirmed upcoming booking owned by the querying customer."""

    booking_id: UUID
    appointment_id: str | None
    start_at: datetime | None
    service_name: str | None


async def find_customer_bookings(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    channel: str,
    customer_handle: str,
) -> list[CustomerBooking]:
    """Return the caller's confirmed, upcoming bookings, soonest first.

    Keyed on the verified ``customer_handle`` — the security anchor.
    """
    rows = await conn.fetch(
        """
        SELECT id, appointment_id, start_at, service_name
          FROM bookings
         WHERE tenant_id = $1
           AND channel = $2
           AND customer_handle = $3
           AND status = 'confirmed'
           AND start_at IS NOT NULL
           AND start_at > now()
         ORDER BY start_at
        """,
        tenant_id,
        channel,
        customer_handle,
    )
    return [
        CustomerBooking(
            booking_id=r["id"],
            appointment_id=r["appointment_id"],
            start_at=r["start_at"],
            service_name=r["service_name"],
        )
        for r in rows
    ]


class BookingNotOwned(Exception):
    """The (booking_id, handle) pair did not match a confirmed row owned
    by this customer — refuse to touch EA."""


async def _load_owned_appointment(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    booking_id: UUID,
    customer_handle: str,
) -> str:
    """Return the EA appointment_id for a booking the customer owns, or
    raise ``BookingNotOwned``. The ownership check is the gate before any
    EA mutation."""
    row = await conn.fetchrow(
        """
        SELECT appointment_id
          FROM bookings
         WHERE id = $1
           AND tenant_id = $2
           AND customer_handle = $3
           AND status = 'confirmed'
        """,
        booking_id,
        tenant_id,
        customer_handle,
    )
    if row is None or row["appointment_id"] is None:
        raise BookingNotOwned(
            "no confirmed booking with that id owned by this customer handle"
        )
    return str(row["appointment_id"])


async def cancel_booking(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    booking_id: UUID,
    customer_handle: str,
) -> bool:
    """Cancel a booking the customer owns. Returns True on success.

    Raises ``BookingNotOwned`` (no EA call made) if the ownership check
    fails — this is the impersonation guard. Propagates EA errors so the
    caller can decide messaging.
    """
    appointment_id = await _load_owned_appointment(
        conn, tenant_id=tenant_id, booking_id=booking_id, customer_handle=customer_handle
    )
    client = get_ea_booking_client()
    try:
        await client.cancel_appointment(
            tenant_id=tenant_id, appointment_id=int(appointment_id)
        )
    except (EABookingTransientError, EABookingStructuralError):
        logger.warning(
            "cancel_booking: EA cancel failed tenant_id=%s booking_id=%s",
            tenant_id,
            booking_id,
        )
        raise
    # Re-assert the ownership predicate in the mutation itself (not just the
    # earlier SELECT) so there is no TOCTOU window where the row could be
    # acted on without the verified handle matching.
    await conn.execute(
        """
        UPDATE bookings
           SET status = 'cancelled', updated_at = now()
         WHERE id = $1 AND tenant_id = $2 AND customer_handle = $3
        """,
        booking_id,
        tenant_id,
        customer_handle,
    )
    return True


async def reschedule_booking(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    booking_id: UUID,
    customer_handle: str,
    new_start: datetime,
    new_end: datetime,
) -> bool:
    """Reschedule a booking the customer owns to a new slot. Returns True.

    Ownership-gated identically to ``cancel_booking``. The slot is
    supplied by the caller (e.g. parsed from an EA reschedule-link
    webhook or an interactive picker); this function performs the EA PUT
    and mirrors the new slot onto the Polaris row.
    """
    appointment_id = await _load_owned_appointment(
        conn, tenant_id=tenant_id, booking_id=booking_id, customer_handle=customer_handle
    )
    client = get_ea_booking_client()
    try:
        await client.reschedule_appointment(
            tenant_id=tenant_id,
            appointment_id=int(appointment_id),
            start_datetime=new_start.isoformat(),
            end_datetime=new_end.isoformat(),
        )
    except (EABookingTransientError, EABookingStructuralError):
        logger.warning(
            "reschedule_booking: EA reschedule failed tenant_id=%s booking_id=%s",
            tenant_id,
            booking_id,
        )
        raise
    # Ownership predicate re-asserted in the mutation (see cancel_booking).
    await conn.execute(
        """
        UPDATE bookings
           SET status = 'rescheduled', start_at = $3, end_at = $4, updated_at = now()
         WHERE id = $1 AND tenant_id = $2 AND customer_handle = $5
        """,
        booking_id,
        tenant_id,
        new_start,
        new_end,
        customer_handle,
    )
    return True
