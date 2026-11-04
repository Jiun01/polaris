"""Booking integration — Phase 7 PR-6.

Bearer-JWT client + public booking URL composer for the polaris-ea-fork
(Easy!Appointments). All booking data lives in the EA fork's MySQL (per
ADR-024 + ADR-048); Polaris reads/writes via the EA REST API.

Modules:
  * ``client.py`` — EABookingClient + public_booking_url composer
"""

from polaris_api.services.booking.client import (
    EABookingClient,
    EABookingConfigError,
    EABookingError,
    EABookingStructuralError,
    EABookingTransientError,
    get_ea_booking_client,
    public_booking_url,
    reset_client_for_testing,
)

__all__ = [
    "EABookingClient",
    "EABookingConfigError",
    "EABookingError",
    "EABookingStructuralError",
    "EABookingTransientError",
    "get_ea_booking_client",
    "public_booking_url",
    "reset_client_for_testing",
]
