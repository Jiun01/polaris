"""Unit tests for the conversational booking-command intent detector and handlers.

Covers:
  * ``detect_booking_command`` — parametrized phrase matching for all intents
    including the new "book" intent; reschedule-beats-schedule disambiguation.
  * ``_do_new_booking`` — link mode (book_link / book_page fallback).
  * ``handle_booking_command`` — book command with no prior booking doesn't
    fall through (regression guard for the pre-gate placement).
  * ``extract_booking_datetime`` / ``extract_customer_name`` — Haiku extractors.
  * ``_compress_ranges`` / ``_match_time`` / ``_normalize_phone`` — picker helpers.
  * ``BookingPickerState`` — to_dict / from_dict round-trip.
  * Multi-turn date → pick → details → confirm → book flow (mocked EA client).

Pure-function and unit-mocked coverage — no real DB, no real LLM, no real EA.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from polaris_api.services.booking.cancel_reschedule import CustomerBooking
from polaris_api.services.booking.conversational import (
    _WEEKDAY_KEYS,
    _compress_ranges,
    _match_time,
    _normalize_phone,
    _pretty_time,
    _render_range_offer,
    _slot_minutes,
    _split_name,
    detect_booking_command,
    handle_booking_command,
)
from polaris_api.services.booking.slot_picker_state import BookingPickerState

# Only async tests need the asyncio mark — applied per-test below, not globally,
# to avoid pytest warning on sync parametrized functions.

# ---------------------------------------------------------------------------
# detect_booking_command — cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "cancel my booking",
        "I want to cancel my appointment please",
        "CANCEL",
        "cancel",
        "  Cancel  ",
        "batal tempahan",
        "取消预约",
        "取消",
    ],
)
def test_detects_cancel(text: str) -> None:
    assert detect_booking_command(text) == "cancel"


# ---------------------------------------------------------------------------
# detect_booking_command — reschedule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "reschedule",
        "RESCHEDULE",
        "can I reschedule my appointment",
        "I need to change my booking",
        "move my appointment to another time",
        "tukar masa",
        "改期",
    ],
)
def test_detects_reschedule(text: str) -> None:
    assert detect_booking_command(text) == "reschedule"


# ---------------------------------------------------------------------------
# detect_booking_command — confirm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["confirm", "CONFIRM", "ok", "yes", "ya", "确认"])
def test_detects_confirm(text: str) -> None:
    assert detect_booking_command(text) == "confirm"


# ---------------------------------------------------------------------------
# detect_booking_command — book (new)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # English phrases (appointment-noun anchored)
        "book an appointment",
        "Book An Appointment",
        "I want to book an appointment",
        "i want to book a slot",
        "make a booking",
        "schedule an appointment",
        "set up an appointment",
        "make an appointment",
        "i'd like to book an appointment",
        "I would like to book a slot",
        # Bahasa Melayu
        "buat temujanji",
        "nak tempah",
        "buat tempahan",
        # 中文
        "我要预约",
        "约时间",
        # Bare tokens (whole message)
        "book",
        "booking",
        "appointment",
        "tempah",
        "预约",
        "預約",
    ],
)
def test_detects_book(text: str) -> None:
    assert detect_booking_command(text) == "book"


def test_reschedule_beats_book_for_schedule_phrase() -> None:
    """'schedule' inside 'reschedule' must not route to new-booking."""
    assert detect_booking_command("I need to reschedule my appointment") == "reschedule"
    assert detect_booking_command("reschedule") == "reschedule"


def test_reschedule_beats_cancel_when_both_present() -> None:
    # "change my booking" is a reschedule phrase; even with "cancel" in
    # the sentence the more specific reschedule intent wins.
    assert detect_booking_command("cancel and change my booking") == "reschedule"


def test_book_bare_token_not_matched_as_substring() -> None:
    # "book" inside a longer sentence without a booking phrase should NOT
    # match — the bare token requires the whole message to be just "book".
    result = detect_booking_command("I read a book last night")
    assert result is None


# ---------------------------------------------------------------------------
# detect_booking_command — non-commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "what are your opening hours?",
        "I want to cancel my subscription",  # no booking-noun context
        "cancelled my plans for the weekend",  # bare token only, not standalone
        "hello",
        "I read a book last night",  # "book" as noun, not standalone
        "i want to book a hotel for my vacation",  # non-appointment "book"
        "can you help me book a flight?",  # non-appointment "book"
        "i'd like to book a table at a restaurant",  # non-appointment "book"
    ],
)
def test_non_commands_return_none(text: str) -> None:
    assert detect_booking_command(text) is None


# ---------------------------------------------------------------------------
# Helpers for handle_booking_command / _do_new_booking tests
# ---------------------------------------------------------------------------

_TENANT_ID = uuid4()
_CONV_ID = uuid4()


def _make_conn(
    *,
    slug: str | None = "acme-clinic",
    booking_config: dict | None = None,
) -> AsyncMock:
    """Fake asyncpg.Connection for handle_booking_command tests."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=slug)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    # Simulate transaction() context manager (used by _do_cancel reminder void).
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _settings_patch(base_url: str = "https://cal.example.com") -> Any:
    settings = MagicMock()
    settings.ea_api_base_url = base_url
    settings.ea_booking_public_url = "https://calendar.example.com"
    settings.public_api_base_url = "https://api.example.com"
    return patch(
        "polaris_api.services.booking.conversational.get_settings",
        return_value=settings,
    )


def _read_booking_config_patch(cfg: dict | None = None) -> Any:
    # read_booking_config is imported locally inside _do_new_booking, so patch
    # at the source module where the function is defined.
    return patch(
        "polaris_api.services.booking.config.read_booking_config",
        new=AsyncMock(return_value=cfg or {}),
    )


# ---------------------------------------------------------------------------
# _do_new_booking — link mode (book_link)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_do_new_booking_link_mode() -> None:
    """Happy path: provider + service set → book_link with polaris_ref."""
    conn = _make_conn()
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "test-jti"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(7, 3)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        _read_booking_config_patch({"inchat_picker": False}),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="i want to book an appointment",
        )

    assert reply is not None
    assert reply.kind == "book_link"
    # The customer reply carries the SHORT link only; service_id/provider_id/
    # polaris_ref/prefill_phone are added server-side by the /b/{jti} redirect.
    assert "https://api.example.com/b/test-jti" in reply.content
    assert "valid for 1 hour" in reply.content.lower()
    assert "polaris_ref" not in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_do_new_booking_emits_short_link() -> None:
    """The customer reply carries the short /b/{jti} link (prefill_phone is
    added by the redirect, not the reply)."""
    conn = _make_conn()
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-wa"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(1, 2)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        _read_booking_config_patch(),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="+60123",
            text="book",
        )

    assert reply is not None
    assert reply.kind == "book_link"
    assert "https://api.example.com/b/jti-wa" in reply.content
    assert "valid for 1 hour" in reply.content.lower()


@pytest.mark.asyncio(loop_scope="function")
async def test_do_new_booking_non_whatsapp_no_prefill() -> None:
    """Non-WhatsApp channel must not include prefill_phone."""
    conn = _make_conn()
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-tg"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(1, 2)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        _read_booking_config_patch(),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="telegram",
            customer_handle="123456",
            text="book an appointment",
        )

    assert reply is not None
    assert reply.kind == "book_link"
    assert "prefill_phone" not in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_do_new_booking_fallback_when_no_provider() -> None:
    """When branch config returns (None, None) and EA list also fails → book_page."""
    conn = _make_conn()

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(None, None)),
        ),
        patch(
            "polaris_api.services.booking.conversational._resolve_provider_service",
            new=AsyncMock(return_value=(None, None)),
        ),
        _read_booking_config_patch(),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="book an appointment",
        )

    assert reply is not None
    assert reply.kind == "book_page"
    assert "polaris_ref" not in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_do_new_booking_no_prior_booking_required() -> None:
    """'book' command does NOT return None when the customer has no existing booking."""
    conn = _make_conn()
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-new"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(5, 10)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        _read_booking_config_patch(),
        # find_customer_bookings is NOT called for the "book" command — patch it
        # to raise so we'd catch any accidental call.
        patch(
            "polaris_api.services.booking.conversational.find_customer_bookings",
            new=AsyncMock(side_effect=AssertionError("should not be called for book")),
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="book an appointment",
        )

    assert reply is not None
    assert reply.kind == "book_link"


@pytest.mark.asyncio(loop_scope="function")
async def test_handle_booking_command_non_bookable_channel_returns_none() -> None:
    """Non-bookable channel (e.g. 'email' before 1b migration) falls through."""
    # NOTE: after migration 116, email IS bookable. We test a truly unknown channel.
    conn = _make_conn()
    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="sms",  # never bookable
        customer_handle="60123456789",
        text="book an appointment",
    )
    assert reply is None


@pytest.mark.asyncio(loop_scope="function")
async def test_handle_booking_command_unrecognized_text_returns_none() -> None:
    conn = _make_conn()
    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text="what are your opening hours?",
    )
    assert reply is None


# ---------------------------------------------------------------------------
# Cancel / confirm slot text — localized to the tenant timezone (not raw UTC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_cancel_message_localizes_slot_to_tenant_timezone() -> None:
    """The cancel confirmation shows the customer's local time, not UTC.

    Regression for a live bug: cancelling a 2:00 PM Asia/Kuala_Lumpur booking
    rendered "06:00 UTC" instead of the tenant-local "14:00".
    """
    conn = _make_conn()
    # 06:00 UTC == 14:00 in Asia/Kuala_Lumpur (UTC+8).
    booking = CustomerBooking(
        booking_id=uuid4(),
        appointment_id="42",
        start_at=datetime(2026, 6, 26, 6, 0, tzinfo=UTC),
        service_name="General",
    )

    with (
        patch(
            "polaris_api.services.booking.conversational.find_customer_bookings",
            new=AsyncMock(return_value=[booking]),
        ),
        patch(
            "polaris_api.services.booking.conversational.cancel_booking",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "polaris_api.services.booking.conversational.cancel_booking_reminders",
            new=AsyncMock(return_value=None),
        ),
        _read_booking_config_patch({"tenant_timezone": "Asia/Kuala_Lumpur"}),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="cancel my booking",
        )

    assert reply is not None
    assert reply.kind == "cancelled"
    assert "14:00 (Asia/Kuala_Lumpur)" in reply.content
    assert "UTC" not in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_cancel_message_falls_back_to_utc_when_tz_unset() -> None:
    """No tenant_timezone configured → UTC fallback (no crash)."""
    conn = _make_conn()
    booking = CustomerBooking(
        booking_id=uuid4(),
        appointment_id="42",
        start_at=datetime(2026, 6, 26, 6, 0, tzinfo=UTC),
        service_name="General",
    )

    with (
        patch(
            "polaris_api.services.booking.conversational.find_customer_bookings",
            new=AsyncMock(return_value=[booking]),
        ),
        patch(
            "polaris_api.services.booking.conversational.cancel_booking",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "polaris_api.services.booking.conversational.cancel_booking_reminders",
            new=AsyncMock(return_value=None),
        ),
        _read_booking_config_patch({}),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="cancel my booking",
        )

    assert reply is not None
    assert reply.kind == "cancelled"
    assert "06:00 (UTC)" in reply.content


# ---------------------------------------------------------------------------
# _do_reschedule — bound link via the EA-API fallback resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_reschedule_mints_bound_link_via_resolver_fallback() -> None:
    """Reschedule emits a signed short link when branch config is empty but the
    resolver yields a provider/service (the EA-API first-item fallback).

    Regression: _do_reschedule used to call branch_config.get_branch_ea_config
    directly (no EA-API fallback), so EA-default tenants always got the bare
    unbound page instead of a real reschedule link.
    """
    conn = _make_conn()
    booking = CustomerBooking(
        booking_id=uuid4(),
        appointment_id="42",
        start_at=datetime(2026, 6, 26, 6, 0, tzinfo=UTC),
        service_name="General",
    )
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-resched"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.find_customer_bookings",
            new=AsyncMock(return_value=[booking]),
        ),
        patch(
            "polaris_api.services.booking.conversational._resolve_provider_service",
            new=AsyncMock(return_value=(6, 13)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        patch(
            "polaris_api.services.booking.conversational.cancel_booking",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "polaris_api.services.booking.conversational.cancel_booking_reminders",
            new=AsyncMock(return_value=None),
        ),
        _read_booking_config_patch(),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="reschedule my appointment",
        )

    assert reply is not None
    assert reply.kind == "reschedule_link"
    assert "https://api.example.com/b/jti-resched" in reply.content
    assert "valid for 1 hour" in reply.content.lower()
    # Bare public page must NOT be what we hand back.
    assert "/book/" not in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_reschedule_still_links_when_old_slot_release_db_errors() -> None:
    """A DB error releasing the old slot must NOT strand the customer.

    Regression: the release catch only caught (BookingNotOwned, EABookingError),
    so an asyncpg.PostgresError from cancel_booking's UPDATE escaped uncaught and
    the customer never got the (already-minted) reschedule link.
    """
    import asyncpg

    conn = _make_conn()
    booking = CustomerBooking(
        booking_id=uuid4(),
        appointment_id="42",
        start_at=datetime(2026, 6, 26, 6, 0, tzinfo=UTC),
        service_name="General",
    )
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-resched-err"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.find_customer_bookings",
            new=AsyncMock(return_value=[booking]),
        ),
        patch(
            "polaris_api.services.booking.conversational._resolve_provider_service",
            new=AsyncMock(return_value=(6, 13)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        patch(
            "polaris_api.services.booking.conversational.cancel_booking",
            new=AsyncMock(side_effect=asyncpg.PostgresError("db down")),
        ),
        _read_booking_config_patch(),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="reschedule my appointment",
        )

    assert reply is not None
    assert reply.kind == "reschedule_link"
    assert "https://api.example.com/b/jti-resched-err" in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_reschedule_falls_back_to_bare_page_when_resolver_empty() -> None:
    """No provider/service anywhere → bare public page, current booking kept."""
    conn = _make_conn()
    booking = CustomerBooking(
        booking_id=uuid4(),
        appointment_id="42",
        start_at=datetime(2026, 6, 26, 6, 0, tzinfo=UTC),
        service_name="General",
    )

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.find_customer_bookings",
            new=AsyncMock(return_value=[booking]),
        ),
        patch(
            "polaris_api.services.booking.conversational._resolve_provider_service",
            new=AsyncMock(return_value=(None, None)),
        ),
        _read_booking_config_patch(),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="reschedule my appointment",
        )

    assert reply is not None
    assert reply.kind == "reschedule_page"
    assert "polaris_ref" not in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_do_new_booking_no_slug_returns_error() -> None:
    """No tenant slug → error kind, no crash."""
    conn = _make_conn(slug=None)

    with _settings_patch():
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="book an appointment",
        )

    assert reply is not None
    assert reply.kind == "error"


# ---------------------------------------------------------------------------
# BookingPickerState — to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


def test_booking_picker_state_round_trip() -> None:
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=["2026-06-24T09:00:00", "2026-06-24T09:30:00"],
        resolved_slot="2026-06-24T09:00:00",
        offset_days=0,
    )
    recovered = BookingPickerState.from_dict(state.to_dict())
    assert recovered.step == "awaiting_confirm"
    assert recovered.service_id == 3
    assert recovered.provider_id == 7
    assert recovered.offered_slots == ["2026-06-24T09:00:00", "2026-06-24T09:30:00"]
    assert recovered.resolved_slot == "2026-06-24T09:00:00"
    assert recovered.offset_days == 0


def test_booking_picker_state_from_dict_none() -> None:
    """from_dict(None) returns default state."""
    state = BookingPickerState.from_dict(None)
    assert state.step == "awaiting_pick"
    assert state.service_id is None
    assert state.provider_id is None
    assert state.offered_slots == []
    assert state.resolved_slot is None
    assert state.offset_days == 0


def test_booking_picker_state_from_dict_defensive() -> None:
    """from_dict handles unexpected types defensively."""
    state = BookingPickerState.from_dict(
        {
            "step": 42,               # wrong type → cast to str
            "service_id": "not-int",  # wrong type → None
            "provider_id": 7.0,       # float → int
            "offered_slots": "bad",   # wrong type → []
            "resolved_slot": None,
            "offset_days": "oops",    # wrong type → 0
        }
    )
    assert state.step == "42"
    assert state.service_id is None
    assert state.provider_id == 7
    assert state.offered_slots == []
    assert state.resolved_slot is None
    assert state.offset_days == 0


# ---------------------------------------------------------------------------
# Multi-turn in-chat picker flow (Phase 2)
# ---------------------------------------------------------------------------


def _make_picker_conn() -> AsyncMock:
    """Fake asyncpg.Connection for picker-flow tests."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="acme-clinic")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn)
    return conn


_OFFERED_SLOTS = [
    "2026-06-24T09:00:00",
    "2026-06-24T09:30:00",
    "2026-06-24T18:00:00",
]


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_awaiting_pick_resolves_slot() -> None:
    """A stated time that matches a slot exactly → awaiting_confirm with that slot."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
        offset_days=0,
    )

    # awaiting_pick now extracts the time via extract_booking_datetime, then
    # matches it locally against the FULL offered set (no cap). 6pm == 18:00.
    # No Chatwoot client → details capture asks for everything (awaiting_details).
    with _extract_datetime_patch(None, "18:00"):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="the 24th 6pm is good",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"
    assert reply.new_picker_state.resolved_slot == "2026-06-24T18:00:00"


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_awaiting_pick_no_time_reprompts_with_ranges() -> None:
    """When no time is understood, re-show the open ranges with state unchanged."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
        offset_days=0,
    )

    with _extract_datetime_patch(None, None):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="hmm not sure",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert "open" in reply.content.lower()  # ranges, not a numbered list
    assert reply.new_picker_state is state  # unchanged


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_awaiting_pick_nearest_when_taken() -> None:
    """A time with no exact slot resolves to the nearest one (substitute note)."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
        offset_days=0,
    )

    # 09:15 isn't offered; nearest is 09:00 or 09:30 (both 15 min away → first wins).
    with _extract_datetime_patch(None, "09:15"):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="around 9.15",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"
    assert reply.new_picker_state.resolved_slot == "2026-06-24T09:00:00"
    assert "nearest" in reply.content.lower()


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_awaiting_pick_earliest_takes_first_slot() -> None:
    """'earliest' picks the first offered slot without an LLM call."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
        offset_days=0,
    )

    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text="earliest",
        picker_state=state,
    )

    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"
    assert reply.new_picker_state.resolved_slot == "2026-06-24T09:00:00"


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_awaiting_confirm_yes_books() -> None:
    """Awaiting confirm + 'yes' → create_appointment called, kind='booked'."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        offset_days=0,
    )

    mock_appt = {"id": 42}
    fake_pool = MagicMock()
    fake_pool_cm = MagicMock()
    fake_pool_cm.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    fake_pool_cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=fake_pool_cm)

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value=mock_appt)

    with (
        # get_ea_booking_client is imported locally — patch at source.
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        # get_service_pool is also imported locally — patch at source.
        patch(
            "polaris_api.db.pools.get_service_pool",
            return_value=fake_pool,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="yes",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "booked"
    assert reply.new_picker_state is None  # state cleared
    mock_ea.create_appointment.assert_called_once()


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_book_uses_service_duration_for_end() -> None:
    """end_datetime is derived from the EA service duration, not a fixed guess."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        offset_days=0,
    )

    fake_pool = MagicMock()
    fake_pool_cm = MagicMock()
    fake_pool_cm.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    fake_pool_cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=fake_pool_cm)

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value={"id": 42})
    # 45-minute service → end must be start + 45 min, not the +1h legacy guess.
    mock_ea.list_services = AsyncMock(return_value=[{"id": 3, "duration": 45}])

    with (
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        patch(
            "polaris_api.db.pools.get_service_pool",
            return_value=fake_pool,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="yes",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "booked"
    end_dt = mock_ea.create_appointment.call_args.kwargs["end_datetime"]
    # 18:00 + 45 min = 18:45 (UTC normalised on the naive slot string).
    assert end_dt.startswith("2026-06-24T18:45:00")


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_awaiting_pick_missing_ids_resets() -> None:
    """A stale picker blob with no service/provider resets instead of calling EA."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=None,
        provider_id=None,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
        offset_days=0,
    )

    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text="the 24th 6pm",
        picker_state=state,
    )

    assert reply is not None
    assert reply.kind == "error"
    assert reply.new_picker_state is None  # state cleared


@pytest.mark.asyncio(loop_scope="function")
async def test_picker_awaiting_confirm_bare_no_reasks() -> None:
    """Final confirm + a bare 'no' (no edit) → re-ask, no booking, state kept."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
        offset_days=0,
    )

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value={"id": 99})

    with patch(
        "polaris_api.services.booking.client.get_ea_booking_client",
        return_value=mock_ea,
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="no",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_confirm"  # stays at confirm
    # create_appointment must NOT have been called
    mock_ea.create_appointment.assert_not_called()


# ---------------------------------------------------------------------------
# Date-first in-chat picker (hybrid date + link)
# ---------------------------------------------------------------------------


def _ea_calendar_patch(calendar: dict | None = None) -> Any:
    """Patch read_ea_calendar (imported locally inside conversational helpers)."""
    return patch(
        "polaris_api.services.booking.config.read_ea_calendar",
        new=AsyncMock(return_value=calendar or {}),
    )


def _extract_datetime_patch(
    date_value: str | None, time_value: str | None = None
) -> Any:
    """Patch the Haiku date+time extractor used inside the picker.

    Returns ``(date, time)`` — the shape of ``extract_booking_datetime``. Most
    flows only state a date, so ``time_value`` defaults to None.
    """
    return patch(
        "polaris_api.services.booking.nlu.extract_booking_datetime",
        new=AsyncMock(return_value=(date_value, time_value)),
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_inchat_picker_on_offers_date_first() -> None:
    """picker ON new-booking → asks for a date AND includes the short link,
    state = awaiting_date (not an immediate link)."""
    conn = _make_conn()
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-date-first"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(7, 3)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        _read_booking_config_patch({"inchat_picker": True}),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="i want to book an appointment",
        )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert "date" in reply.content.lower()
    assert "https://api.example.com/b/jti-date-first" in reply.content
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_date"
    assert reply.new_picker_state.service_id == 3
    assert reply.new_picker_state.provider_id == 7


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_date_offers_local_time_slots() -> None:
    """A date reply fetches that date's EA hours and offers provider-tz ISO slots."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_date",
        service_id=3,
        provider_id=7,
        offered_slots=[],
        resolved_slot=None,
        offset_days=0,
    )

    mock_ea = AsyncMock()
    # EA returns provider-tz time-only strings for the single requested date.
    mock_ea.list_availabilities = AsyncMock(return_value=["10:00", "14:00"])

    with (
        _extract_datetime_patch("2026-06-27"),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="27 Jun please",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_pick"
    # HH:MM → provider-tz ISO (KL is UTC+8) — no skew.
    assert reply.new_picker_state.offered_slots == [
        "2026-06-27T10:00:00+08:00",
        "2026-06-27T14:00:00+08:00",
    ]


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_date_unparseable_reprompts() -> None:
    """An unparseable date keeps the customer in awaiting_date (re-prompt)."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_date",
        service_id=3,
        provider_id=7,
        offered_slots=[],
        resolved_slot=None,
        offset_days=0,
    )

    with (
        _extract_datetime_patch(None),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="uhh whenever",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_date"


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_date_working_day_empty_says_fully_booked() -> None:
    """Working day with no slots → 'fully booked' + nearest opening → details capture."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_date",
        service_id=3,
        provider_id=7,
        offered_slots=[],
        resolved_slot=None,
        offset_days=0,
    )

    chosen = "2026-06-26"
    weekday_key = _WEEKDAY_KEYS[date.fromisoformat(chosen).weekday()]
    working_plan = {weekday_key: {"start": "09:00", "end": "17:00", "breaks": []}}

    mock_ea = AsyncMock()
    # chosen date empty, next day empty, then a day with a slot.
    mock_ea.list_availabilities = AsyncMock(side_effect=[[], [], ["10:00"]])

    with (
        _extract_datetime_patch(chosen),
        _ea_calendar_patch(
            {"timezone": "Asia/Kuala_Lumpur", "working_plan": working_plan}
        ),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="26 Jun",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert "fully booked" in reply.content.lower()
    assert reply.new_picker_state is not None
    # Nearest opening now flows through the details capture (not a bare yes/no),
    # so a "yes" later books with real details rather than a placeholder.
    assert reply.new_picker_state.step == "awaiting_details"
    # Nearest opening is 2 days on (28 Jun) at 10:00 KL — carries the +08:00 offset.
    assert reply.new_picker_state.resolved_slot == "2026-06-28T10:00:00+08:00"
    # The nearest slot is carried as the single offered slot (for later time edits).
    assert reply.new_picker_state.offered_slots == ["2026-06-28T10:00:00+08:00"]
    # D1: nearest-opening lead uses the picker's 12h format, not 24h+tz.
    assert "10:00 AM" in reply.content
    assert "(Asia/Kuala_Lumpur)" not in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_date_closed_day_says_closed() -> None:
    """Non-working day with no slots → 'closed' wording (not 'fully booked')."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_date",
        service_id=3,
        provider_id=7,
        offered_slots=[],
        resolved_slot=None,
        offset_days=0,
    )

    chosen = "2026-06-28"
    weekday_key = _WEEKDAY_KEYS[date.fromisoformat(chosen).weekday()]
    working_plan = {weekday_key: None}  # closed that weekday

    mock_ea = AsyncMock()
    mock_ea.list_availabilities = AsyncMock(side_effect=[[], ["09:00"]])

    with (
        _extract_datetime_patch(chosen),
        _ea_calendar_patch(
            {"timezone": "Asia/Kuala_Lumpur", "working_plan": working_plan}
        ),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="28 Jun",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert "closed" in reply.content.lower()
    assert "fully booked" not in reply.content.lower()


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_date_no_openings_in_window_sends_link() -> None:
    """Nothing opens within the search window → booking link + cleared state."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_date",
        service_id=3,
        provider_id=7,
        offered_slots=[],
        resolved_slot=None,
        offset_days=0,
    )

    pending_booking = MagicMock()
    pending_booking.jti = "jti-nowindow"

    mock_ea = AsyncMock()
    mock_ea.list_availabilities = AsyncMock(return_value=[])  # always empty

    with (
        _settings_patch(),
        _extract_datetime_patch("2026-06-26"),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="26 Jun",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "book_link"
    assert "https://api.example.com/b/jti-nowindow" in reply.content
    assert reply.new_picker_state is None  # picker cleared


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_pick_another_date_returns_to_date_prompt() -> None:
    """'another date' in awaiting_pick routes back to awaiting_date."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=["2026-06-27T10:00:00+08:00"],
        resolved_slot=None,
        offset_days=0,
    )

    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text="another date",
        picker_state=state,
    )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_date"
    assert reply.new_picker_state.offered_slots == []


# ---------------------------------------------------------------------------
# Availability ranges + nearest-time matching (pure helpers)
# ---------------------------------------------------------------------------


def test_slot_minutes_and_pretty_time() -> None:
    assert _slot_minutes("2026-06-30T14:00:00+08:00") == 14 * 60
    assert _slot_minutes("not-a-slot") is None
    assert _pretty_time("14:00") == "2:00 PM"
    assert _pretty_time("00:30") == "12:30 AM"
    assert _pretty_time("2026-06-30T09:15:00+08:00") == "9:15 AM"
    assert _pretty_time("garbage") == "garbage"  # fail-soft


def test_compress_ranges_contiguous() -> None:
    """A contiguous 15-min run → one range ending at last_start + step."""
    slots = [
        f"2026-06-30T{h:02d}:{m:02d}:00+08:00"
        for h in (10, 11)
        for m in (0, 15, 30, 45)
    ]
    # 10:00..11:45 step 15 → (600, 720) i.e. 10:00 AM–12:00 PM
    assert _compress_ranges(slots) == [(600, 720)]


def test_compress_ranges_splits_on_booked_hole() -> None:
    """A booked mid-day gap splits the day into two ranges."""
    starts = ["10:00", "10:15", "10:30", "13:00", "13:15"]  # 10:45–13:00 booked
    slots = [f"2026-06-30T{s}:00+08:00" for s in starts]
    # run1: 10:00–10:30 step15 → (600, 645); run2: 13:00–13:15 → (780, 810)
    assert _compress_ranges(slots) == [(600, 645), (780, 810)]


def test_compress_ranges_single_slot_uses_default_duration() -> None:
    assert _compress_ranges(["2026-06-30T10:00:00+08:00"]) == [(600, 630)]
    assert _compress_ranges([]) == []


def test_render_range_offer_two_windows() -> None:
    starts = ["10:00", "10:15", "10:30", "13:00", "13:15"]
    slots = [f"2026-06-30T{s}:00+08:00" for s in starts]
    text = _render_range_offer(slots)
    assert "we're open" in text
    assert "10:00 AM" in text
    assert " and " in text  # two windows joined
    assert "what time works" in text.lower()


def test_match_time_exact_and_nearest() -> None:
    slots = ["2026-06-30T14:00:00+08:00", "2026-06-30T14:15:00+08:00"]
    assert _match_time(slots, "14:00") == ("2026-06-30T14:00:00+08:00", True)
    # 14:05 is nearest to 14:00 (5 < 10)
    assert _match_time(slots, "14:05") == ("2026-06-30T14:00:00+08:00", False)
    # no parseable slots / bad target → (None, False)
    assert _match_time([], "14:00") == (None, False)
    assert _match_time(slots, "nope") == (None, False)


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_date_with_time_goes_to_details() -> None:
    """'30 Jun at 2pm' matches the day's 14:00 slot and flows to details capture."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_date",
        service_id=3,
        provider_id=7,
        offered_slots=[],
        resolved_slot=None,
        offset_days=0,
    )

    mock_ea = AsyncMock()
    # Full day incl. noon + afternoon — the old [:5] cap would have hidden 14:00.
    mock_ea.list_availabilities = AsyncMock(
        return_value=["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]
    )

    with (
        _extract_datetime_patch("2026-06-30", "14:00"),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="30 jun at 2pm",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"
    assert reply.new_picker_state.resolved_slot == "2026-06-30T14:00:00+08:00"
    # Full day persisted (not capped) for time edits at the final confirm.
    assert len(reply.new_picker_state.offered_slots) == 7


@pytest.mark.asyncio(loop_scope="function")
async def test_awaiting_date_no_time_shows_ranges() -> None:
    """A date with no time shows open ranges and waits in awaiting_pick."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_date",
        service_id=3,
        provider_id=7,
        offered_slots=[],
        resolved_slot=None,
        offset_days=0,
    )

    mock_ea = AsyncMock()
    mock_ea.list_availabilities = AsyncMock(return_value=["10:00", "10:30", "11:00"])

    with (
        _extract_datetime_patch("2026-06-30", None),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="30 jun",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert "open" in reply.content.lower()
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_pick"
    assert len(reply.new_picker_state.offered_slots) == 3


# ---------------------------------------------------------------------------
# Details capture (Part B) — Name + Email + Phone, hybrid parse + final confirm
# ---------------------------------------------------------------------------


def _extract_name_patch(value: str | None) -> Any:
    """Patch the Haiku name extractor used inside the details capture."""
    return patch(
        "polaris_api.services.booking.nlu.extract_customer_name",
        new=AsyncMock(return_value=value),
    )


def test_normalize_phone_my_formats() -> None:
    assert _normalize_phone("0123456789") == "0123456789"
    assert _normalize_phone("+60123456789") == "+60123456789"
    assert _normalize_phone("012 345 6789") == "0123456789"
    assert _normalize_phone("012-345-6789") == "0123456789"
    assert _normalize_phone("123") is None  # too short
    assert _normalize_phone("not a phone") is None


def test_split_name() -> None:
    assert _split_name("John Tan") == ("John", "Tan")
    assert _split_name("Cher") == ("Cher", "")
    assert _split_name("Mary Jane Watson") == ("Mary", "Jane Watson")
    assert _split_name(None) == ("", "")
    assert _split_name("   ") == ("", "")


@pytest.mark.asyncio(loop_scope="function")
async def test_details_combined_reply_reaches_confirm() -> None:
    """One message with name+email+phone → final summary (awaiting_confirm)."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_details",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
    )

    with _extract_name_patch("John Tan"):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="John Tan, john@example.com, 0123456789",
            picker_state=state,
        )

    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_confirm"
    assert reply.new_picker_state.customer_name == "John Tan"
    assert reply.new_picker_state.customer_email == "john@example.com"
    assert reply.new_picker_state.customer_phone == "0123456789"
    assert "confirm" in reply.content.lower()


@pytest.mark.asyncio(loop_scope="function")
async def test_details_email_required_reasks() -> None:
    """name + phone but no email → re-ask email, never reaching confirm."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_details",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_phone="+60123456789",  # already have phone
    )

    with _extract_name_patch("John Tan"):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="I'm John Tan",
            picker_state=state,
        )

    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"  # still collecting
    assert reply.new_picker_state.customer_name == "John Tan"
    assert reply.new_picker_state.customer_email is None
    # D2: re-ask reads "I still need your email." (not "I still need what's your…")
    assert "I still need your" in reply.content
    assert "email" in reply.content.lower()


@pytest.mark.asyncio(loop_scope="function")
async def test_details_prefill_from_chatwoot_skips_phone() -> None:
    """A matched time prefetches the Chatwoot contact; a known phone isn't asked."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
    )

    cw = AsyncMock()
    cw.get_contact_details = AsyncMock(return_value={"phone": "+60123456789"})

    # "6pm" matches the 18:00 slot → _begin_details fetches the contact once.
    with _extract_datetime_patch(None, "18:00"):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="6pm",
            picker_state=state,
            chatwoot=cw,
            cw_account_id=1,
            cw_conv_id=2,
        )

    cw.get_contact_details.assert_awaited_once_with(account_id=1, conversation_id=2)
    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"
    assert reply.new_picker_state.customer_phone == "+60123456789"
    assert "name" in reply.content.lower()
    assert "email" in reply.content.lower()
    assert "phone" not in reply.content.lower()  # phone already known


@pytest.mark.asyncio(loop_scope="function")
async def test_confirm_yes_books_with_real_details() -> None:
    """Final confirm + free-flow yes → create_appointment with collected details."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
    )

    fake_pool = MagicMock()
    fake_pool_cm = MagicMock()
    fake_pool_cm.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    fake_pool_cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=fake_pool_cm)

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value={"id": 42})

    with (
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        patch("polaris_api.db.pools.get_service_pool", return_value=fake_pool),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="yeah sure ok",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "booked"
    assert reply.new_picker_state is None
    customer = mock_ea.create_appointment.call_args.kwargs["customer"]
    assert customer["first_name"] == "John"
    assert customer["last_name"] == "Tan"
    assert customer["email"] == "john@example.com"
    assert customer["phone"] == "+60123456789"


@pytest.mark.asyncio(loop_scope="function")
async def test_confirm_edit_time_resummarizes_no_book() -> None:
    """'actually 5pm' at the final confirm re-resolves the time, does NOT book."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=[
            "2026-06-24T09:00:00",
            "2026-06-24T17:00:00",
            "2026-06-24T18:00:00",
        ],
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
    )

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock()

    with (
        _extract_datetime_patch(None, "17:00"),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="actually make it 5pm",
            picker_state=state,
        )

    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_confirm"
    assert reply.new_picker_state.resolved_slot == "2026-06-24T17:00:00"
    mock_ea.create_appointment.assert_not_called()


@pytest.mark.asyncio(loop_scope="function")
async def test_confirm_edit_email_resummarizes_no_book() -> None:
    """'my email is bob@x.com' at the final confirm updates the email, no book."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
    )

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock()

    with patch(
        "polaris_api.services.booking.client.get_ea_booking_client",
        return_value=mock_ea,
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="my email is bob@x.com",
            picker_state=state,
        )

    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_confirm"
    assert reply.new_picker_state.customer_email == "bob@x.com"
    assert "bob@x.com" in reply.content
    mock_ea.create_appointment.assert_not_called()


@pytest.mark.asyncio(loop_scope="function")
async def test_details_attempts_cap_hands_link() -> None:
    """After too many failed detail turns, hand the booking link + clear state."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_details",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_phone="+60123456789",  # only email missing
        detail_attempts=3,  # one more failure trips the cap
    )

    pending_booking = MagicMock()
    pending_booking.jti = "jti-details-cap"

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="no email sorry",  # still no valid email
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "book_link"
    assert "https://api.example.com/b/jti-details-cap" in reply.content
    assert reply.new_picker_state is None  # picker cleared


# ---------------------------------------------------------------------------
# Streamline fixes (v1.0.71): bail at every step, navigation in details, copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_bail_at_awaiting_pick_clears() -> None:
    """'cancel' at the ranges step exits the picker (was: re-showed ranges)."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
    )
    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text="cancel",
        picker_state=state,
    )
    assert reply is not None
    assert reply.kind == "error"
    assert reply.new_picker_state is None


@pytest.mark.asyncio(loop_scope="function")
async def test_bail_at_awaiting_confirm_clears() -> None:
    """'stop' at the final summary exits the picker without booking."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
    )
    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock()
    with patch(
        "polaris_api.services.booking.client.get_ea_booking_client",
        return_value=mock_ea,
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="stop",
            picker_state=state,
        )
    assert reply is not None
    assert reply.kind == "error"
    assert reply.new_picker_state is None
    mock_ea.create_appointment.assert_not_called()


@pytest.mark.asyncio(loop_scope="function")
async def test_confirm_explicit_no_offers_graceful_exit() -> None:
    """An explicit 'no' at confirm acknowledges + offers change/cancel, no booking."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
    )
    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock()
    with patch(
        "polaris_api.services.booking.client.get_ea_booking_client",
        return_value=mock_ea,
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="no",
            picker_state=state,
        )
    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_confirm"
    assert "different time" in reply.content.lower()
    assert "cancel" in reply.content.lower()
    mock_ea.create_appointment.assert_not_called()


@pytest.mark.asyncio(loop_scope="function")
async def test_details_another_date_reroutes() -> None:
    """'another date' while collecting details re-routes to the date prompt."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_details",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
    )
    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text="another date",
        picker_state=state,
    )
    assert reply is not None
    assert reply.kind == "slot_offer"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_date"
    assert "date and time" in reply.content.lower()


@pytest.mark.asyncio(loop_scope="function")
async def test_details_bare_date_reroutes_to_ranges() -> None:
    """A bare new date typed in details re-handles availability (not a failed attempt)."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_details",
        service_id=3,
        provider_id=7,
        offered_slots=["2026-06-24T18:00:00"],
        resolved_slot="2026-06-24T18:00:00",
    )
    mock_ea = AsyncMock()
    mock_ea.list_availabilities = AsyncMock(return_value=["10:00", "10:30", "11:00"])

    with (
        _extract_datetime_patch("2026-06-29", None),  # the customer's "29 june"
        _extract_name_patch(None),  # nothing name-like in the message
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="29 june",
            picker_state=state,
        )
    assert reply is not None
    assert reply.kind == "slot_offer"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_pick"
    assert "open" in reply.content.lower()


@pytest.mark.asyncio(loop_scope="function")
async def test_details_month_name_not_rerouted() -> None:
    """A one-word name that's a month ("May") is captured, NOT mistaken for a date.

    The reroute is gated on the message having a digit, so a digit-free name never
    triggers a date re-route (and no EA/datetime call happens).
    """
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_details",
        service_id=3,
        provider_id=7,
        offered_slots=["2026-06-24T18:00:00"],
        resolved_slot="2026-06-24T18:00:00",
    )
    # If the gate were LLM-based this would reroute; the digit gate must not.
    with _extract_name_patch("May"):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="May",
            picker_state=state,
        )
    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"  # NOT rerouted to date
    assert reply.new_picker_state.customer_name == "May"
    assert "i still need your" in reply.content.lower()


def test_ask_for_details_single_your() -> None:
    """The details ask uses one 'your', not one per field."""
    from polaris_api.services.booking.conversational import _ask_for_details

    text = _ask_for_details(["name", "email", "phone"])
    assert text == "what's your name, email and phone number?"
    assert "your email" not in text  # not "your name, your email…"


@pytest.mark.asyncio(loop_scope="function")
async def test_booked_message_has_no_double_for() -> None:
    """The booked confirmation reads '<name>, you're confirmed for <slot>' (no 'for…for')."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=["2026-06-24T18:00:00"],
        resolved_slot="2026-06-24T18:00:00",
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
    )
    fake_pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=cm)
    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value={"id": 1})
    with (
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        patch("polaris_api.db.pools.get_service_pool", return_value=fake_pool),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="yes",
            picker_state=state,
        )
    assert "John Tan, you're confirmed for" in reply.content
    assert "for John Tan" not in reply.content  # no double-"for"


def test_agent_prompt_has_booking_guardrail() -> None:
    """The agent system prompt forbids inventing availability / slot lists."""
    from polaris_api.services._llm_prompts import build_agent_system_prompt

    prompt = build_agent_system_prompt("simple", "en")
    assert "cannot see live availability" in prompt.lower()
    assert "slot lists" in prompt.lower()
    assert "book an appointment" in prompt.lower()


# ---------------------------------------------------------------------------
# Pre-agent availability/date routing (Finding 2 structural fix)
# ---------------------------------------------------------------------------


def _classify_patch(value: str | None) -> Any:
    """Patch the Haiku booking-intent classifier."""
    return patch(
        "polaris_api.services.booking.nlu.classify_booking_intent",
        new=AsyncMock(return_value=value),
    )


def test_looks_date_ish() -> None:
    from polaris_api.services.booking.conversational import _looks_date_ish

    for pos in ["1 july", "5 July", "july 1", "next monday", "tomorrow", "5/7", "30-6"]:
        assert _looks_date_ish(pos), pos
    for neg in ["0123456789", "hello", "you may", "book a table", "my order"]:
        assert not _looks_date_ish(neg), neg


def test_looks_date_ish_malay() -> None:
    """Bahasa Melayu months (incl. non-English-prefix mac/mei/ogos/okt/dis),
    weekdays, and relative-day cues are recognized."""
    from polaris_api.services.booking.conversational import _looks_date_ish

    for pos in [
        "5 ogos",
        "1 mei",
        "3 mac",
        "10 okt",
        "25 dis",
        "isnin depan",
        "esok",
        "lusa",
        "minggu depan",
        "hari khamis",
    ]:
        assert _looks_date_ish(pos), pos
    for neg in ["apa khabar", "saya nak tempah", "terima kasih"]:
        assert not _looks_date_ish(neg), neg


def test_looks_date_ish_cjk() -> None:
    """CJK date cues (numeric/Chinese-numeral months, relative days, weekdays)
    are recognized — including when preceded by other CJK chars (no \\b boundary)."""
    from polaris_api.services.booking.conversational import _looks_date_ish

    for pos in [
        "7月1日",
        "我要7月1日",  # preceded by CJK — the \b gotcha
        "明天",
        "今天",
        "后天",
        "下星期一",
        "下周",
        "星期三",
        "周五",
        "礼拜二",
        "十月",
    ]:
        assert _looks_date_ish(pos), pos
    for neg in ["你好", "谢谢", "我的订单"]:
        assert not _looks_date_ish(neg), neg


@pytest.mark.asyncio(loop_scope="function")
async def test_bare_date_picker_off_returns_none() -> None:
    """A bare date on a picker-DISABLED tenant is NOT intercepted (no classify call)."""
    conn = _make_conn()
    classify = AsyncMock(return_value="book")
    with (
        _read_booking_config_patch({"inchat_picker": False}),
        patch("polaris_api.services.booking.nlu.classify_booking_intent", new=classify),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="1 july",
        )
    assert reply is None
    classify.assert_not_called()  # date-ish gate requires inchat_picker on


@pytest.mark.asyncio(loop_scope="function")
async def test_nonbooking_date_mention_returns_none() -> None:
    """A mentioned-but-not-booking date → classifier null → falls to the agent."""
    conn = _make_conn()
    with (
        _read_booking_config_patch({"inchat_picker": True}),
        _classify_patch(None),  # classifier's false-positive guard
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="my invoice from June 1 is wrong",
        )
    assert reply is None


@pytest.mark.asyncio(loop_scope="function")
async def test_availability_keyword_fires_classifier_regardless_of_picker() -> None:
    """An availability keyword fires the classifier without the picker-on gate."""
    conn = _make_conn()
    classify = AsyncMock(return_value=None)
    with patch("polaris_api.services.booking.nlu.classify_booking_intent", new=classify):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="are you available on tuesday",
        )
    assert reply is None  # classifier returned None
    classify.assert_awaited_once()  # the keyword path fired it


@pytest.mark.asyncio(loop_scope="function")
async def test_bare_date_picker_on_jumps_to_ranges() -> None:
    """A bare date on a picker-ON tenant routes to the picker → that date's ranges."""
    conn = _make_conn()
    mock_ea = AsyncMock()
    mock_ea.list_availabilities = AsyncMock(return_value=["09:00", "09:30", "10:00"])
    # Explicit mock so we can assert the gate's booking_config read is REUSED by
    # _do_new_booking (read once, not twice) on the bare-date→book path.
    read_cfg = AsyncMock(return_value={"inchat_picker": True})

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.config.read_booking_config", new=read_cfg
        ),
        _classify_patch("book"),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(7, 3)),
        ),
        _extract_datetime_patch("2026-07-01", None),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="1 july",
        )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_pick"  # jumped past "what date?"
    assert "open" in reply.content.lower()  # real ranges, not the agent
    assert reply.new_picker_state.service_id == 3
    assert reply.new_picker_state.provider_id == 7
    assert read_cfg.await_count == 1  # gate read reused, not duplicated


# ---------------------------------------------------------------------------
# extract_booking_datetime (Haiku date+time extractor)
# ---------------------------------------------------------------------------


def _llm_extract_patches(extract_text: str) -> tuple[Any, Any, Any]:
    """The three patches every Haiku-helper unit test needs (client / text / model).

    Patched at ``polaris_api.services.booking.nlu`` — that module imports these
    3 names directly from ``polaris_api.services.llm`` (``from ... import
    _get_anthropic_client, ...``), so patching them at the `llm` module's own
    attribute would not affect `nlu`'s already-bound local names.
    """
    mock_message = MagicMock()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)
    return (
        patch(
            "polaris_api.services.booking.nlu._get_anthropic_client",
            return_value=mock_client,
        ),
        patch(
            "polaris_api.services.booking.nlu._extract_text", return_value=extract_text
        ),
        patch(
            "polaris_api.services.booking.nlu._model_for_intent",
            return_value=("claude-haiku-4-5", "haiku", None),
        ),
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_booking_datetime_date_and_time() -> None:
    """A clear future date + time is parsed into (date, HH:MM)."""
    from polaris_api.services.booking.nlu import extract_booking_datetime

    c, t, m = _llm_extract_patches('{"date": "2026-06-30", "time": "14:00"}')
    with c, t, m:
        result = await extract_booking_datetime(
            "30 jun at 2pm", today_iso="2026-06-25", timezone="Asia/Kuala_Lumpur"
        )

    assert result == ("2026-06-30", "14:00")


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_booking_datetime_date_only() -> None:
    """A date with no time → (date, None)."""
    from polaris_api.services.booking.nlu import extract_booking_datetime

    c, t, m = _llm_extract_patches('{"date": "2026-06-30", "time": null}')
    with c, t, m:
        result = await extract_booking_datetime(
            "the 30th", today_iso="2026-06-25", timezone="Asia/Kuala_Lumpur"
        )

    assert result == ("2026-06-30", None)


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_booking_datetime_rejects_past_date_keeps_time() -> None:
    """A past date is dropped but a valid time survives (independent validation)."""
    from polaris_api.services.booking.nlu import extract_booking_datetime

    c, t, m = _llm_extract_patches('{"date": "2026-06-01", "time": "09:30"}')
    with c, t, m:
        result = await extract_booking_datetime(
            "june 1 at 9.30am", today_iso="2026-06-25", timezone="Asia/Kuala_Lumpur"
        )

    assert result == (None, "09:30")


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_booking_datetime_bad_time_keeps_date() -> None:
    """A malformed time is dropped but a valid date survives."""
    from polaris_api.services.booking.nlu import extract_booking_datetime

    c, t, m = _llm_extract_patches('{"date": "2026-06-30", "time": "25:99"}')
    with c, t, m:
        result = await extract_booking_datetime(
            "30 jun", today_iso="2026-06-25", timezone="UTC"
        )

    assert result == ("2026-06-30", None)


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_booking_datetime_null_response() -> None:
    """No date or time in the message → (None, None)."""
    from polaris_api.services.booking.nlu import extract_booking_datetime

    c, t, m = _llm_extract_patches('{"date": null, "time": null}')
    with c, t, m:
        result = await extract_booking_datetime(
            "do you have parking?", today_iso="2026-06-25", timezone="UTC"
        )

    assert result == (None, None)


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_booking_datetime_llm_failure_returns_none_pair() -> None:
    """Any exception from the LLM → (None, None) (fail-safe)."""
    from polaris_api.services.booking.nlu import extract_booking_datetime

    with patch(
        "polaris_api.services.booking.nlu._get_anthropic_client",
        side_effect=RuntimeError("API down"),
    ):
        result = await extract_booking_datetime(
            "tomorrow at 3pm", today_iso="2026-06-25", timezone="UTC"
        )

    assert result == (None, None)


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_customer_name_returns_name() -> None:
    """A clear name is parsed and returned."""
    from polaris_api.services.booking.nlu import extract_customer_name

    c, t, m = _llm_extract_patches('{"name": "John Tan"}')
    with c, t, m:
        result = await extract_customer_name("hi i'm John Tan, john@x.com")

    assert result == "John Tan"


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_customer_name_null_returns_none() -> None:
    """No clear name → None."""
    from polaris_api.services.booking.nlu import extract_customer_name

    c, t, m = _llm_extract_patches('{"name": null}')
    with c, t, m:
        result = await extract_customer_name("0123456789")

    assert result is None


@pytest.mark.asyncio(loop_scope="function")
async def test_extract_customer_name_llm_failure_returns_none() -> None:
    """Any exception from the LLM → None (fail-safe)."""
    from polaris_api.services.booking.nlu import extract_customer_name

    with patch(
        "polaris_api.services.booking.nlu._get_anthropic_client",
        side_effect=RuntimeError("API down"),
    ):
        result = await extract_customer_name("John Tan")

    assert result is None


# ---------------------------------------------------------------------------
# Hybrid intent detection — broadened phrase table (regex baseline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Desire + appointment-noun combos the table now catches deterministically.
        ("hi i would like a booking", "book"),
        ("i would like a booking", "book"),
        ("i want a booking", "book"),
        ("i need a booking", "book"),
        ("i want an appointment", "book"),
        ("i'd like an appointment", "book"),
        ("i need an appointment", "book"),
    ],
)
def test_detect_broadened_phrases(text: str, expected: str) -> None:
    assert detect_booking_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Booking keyword present but NOT a deterministic command — must stay None
        # (high-precision regex). The gated LLM is the precise filter for these:
        # ambiguous verbs and non-appointment uses.
        "i read your facebook post",
        "can you book a table at the restaurant next door",
        "i have an appointment already right",
        "i want to book a hotel for my vacation",
        "i want to cancel my subscription",
        # Ambiguous verb phrasings deliberately left to the LLM fallback.
        "i want to book",
        "i need to book",
        "i want to cancel",
    ],
)
def test_detect_no_false_positive_on_substring(text: str) -> None:
    assert detect_booking_command(text) is None


@pytest.mark.parametrize(
    "text,ish",
    [
        ("i would like a booking", True),
        ("any appointment slots?", True),
        ("nak tempah", True),
        ("取消预约", True),
        ("what are your opening hours", False),
        ("do you sell shampoo", False),
    ],
)
def test_looks_booking_ish(text: str, ish: bool) -> None:
    from polaris_api.services.booking.conversational import _looks_booking_ish

    assert _looks_booking_ish(text) is ish


# ---------------------------------------------------------------------------
# Hybrid intent detection — gated LLM fallback (edge cases)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_llm_fallback_fires_on_booking_ish_miss() -> None:
    """A booking-ish message the phrase table misses calls the LLM, which routes."""
    conn = _make_conn()
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-fallback"

    classify = AsyncMock(return_value="book")
    with (
        _settings_patch(),
        patch("polaris_api.services.booking.nlu.classify_booking_intent", new=classify),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(7, 3)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        _read_booking_config_patch({"inchat_picker": False}),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            # Phrasing not in any phrase table, but contains "appointment".
            text="is it possible to get myself an appointment with someone",
        )

    classify.assert_awaited_once()
    assert reply is not None
    assert reply.kind == "book_link"


@pytest.mark.asyncio(loop_scope="function")
async def test_llm_fallback_skipped_when_not_booking_ish() -> None:
    """A message with no booking keyword never triggers an LLM call (gate)."""
    conn = _make_conn()
    classify = AsyncMock(return_value="book")
    with patch("polaris_api.services.booking.nlu.classify_booking_intent", new=classify):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="what are your opening hours?",
        )

    classify.assert_not_awaited()
    assert reply is None


@pytest.mark.asyncio(loop_scope="function")
async def test_llm_fallback_not_called_when_phrase_matches() -> None:
    """A phrase-table hit short-circuits — the LLM is never called."""
    conn = _make_conn()
    pending_booking = MagicMock()
    pending_booking.booking_id = uuid4()
    pending_booking.jti = "jti-regex"
    classify = AsyncMock(return_value="book")
    with (
        _settings_patch(),
        patch("polaris_api.services.booking.nlu.classify_booking_intent", new=classify),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(7, 3)),
        ),
        patch(
            "polaris_api.services.booking.conversational.ensure_binding_configured",
            return_value=None,
        ),
        patch(
            "polaris_api.services.booking.conversational.link_flow"
            ".create_pending_booking",
            new=AsyncMock(return_value=pending_booking),
        ),
        _read_booking_config_patch({"inchat_picker": False}),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="i want to book an appointment",
        )

    classify.assert_not_awaited()
    assert reply is not None
    assert reply.kind == "book_link"


@pytest.mark.asyncio(loop_scope="function")
async def test_llm_fallback_none_falls_through() -> None:
    """Booking-ish but the LLM says not-a-command → fall through (None)."""
    conn = _make_conn()
    classify = AsyncMock(return_value=None)
    with patch("polaris_api.services.booking.nlu.classify_booking_intent", new=classify):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="i read your facebook post earlier",
        )

    classify.assert_awaited_once()
    assert reply is None


@pytest.mark.asyncio(loop_scope="function")
async def test_llm_fallback_skipped_on_non_bookable_channel() -> None:
    """Even booking-ish text doesn't call the LLM on a non-bookable channel."""
    conn = _make_conn()
    classify = AsyncMock(return_value="book")
    with patch("polaris_api.services.booking.nlu.classify_booking_intent", new=classify):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="sms",  # never bookable
            customer_handle="60123456789",
            text="i would like a booking please",
        )

    classify.assert_not_awaited()
    assert reply is None


# ---------------------------------------------------------------------------
# classify_booking_intent (Haiku edge-case classifier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"intent": "book"}', "book"),
        ('{"intent": "cancel"}', "cancel"),
        ('{"intent": "reschedule"}', "reschedule"),
        ('{"intent": "confirm"}', "confirm"),
        ('{"intent": null}', None),
        ('{"intent": "chitchat"}', None),  # hallucinated label rejected
    ],
)
@pytest.mark.asyncio(loop_scope="function")
async def test_classify_booking_intent(raw: str, expected: str | None) -> None:
    from polaris_api.services.booking.nlu import classify_booking_intent

    mock_message = MagicMock()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    with (
        patch(
            "polaris_api.services.booking.nlu._get_anthropic_client",
            return_value=mock_client,
        ),
        patch("polaris_api.services.booking.nlu._extract_text", return_value=raw),
        patch(
            "polaris_api.services.booking.nlu._model_for_intent",
            return_value=("claude-haiku-4-5", "haiku", None),
        ),
    ):
        result = await classify_booking_intent("i'd like a booking")

    assert result == expected


@pytest.mark.asyncio(loop_scope="function")
async def test_classify_booking_intent_llm_failure_returns_none() -> None:
    from polaris_api.services.booking.nlu import classify_booking_intent

    with patch(
        "polaris_api.services.booking.nlu._get_anthropic_client",
        side_effect=RuntimeError("API down"),
    ):
        result = await classify_booking_intent("i'd like a booking")

    assert result is None


# ---------------------------------------------------------------------------
# Multilingual fixes (中文 + Bahasa Melayu) — F1 last_name, F2 date-ish, F3 nav
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_confirm_yes_books_single_token_name_falls_back_to_unknown() -> None:
    """F1: a single-token name (e.g. a Chinese name) books with last_name='Unknown'.

    EA's create_appointment rejects a blank last_name, so an all-Chinese name or a
    mononym must fall back to 'Unknown' — the customer never sees it (chat uses
    customer_name)."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="王芳",
        customer_email="wangfang@example.com",
        customer_phone="+60198887777",
    )

    fake_pool = MagicMock()
    fake_pool_cm = MagicMock()
    fake_pool_cm.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    fake_pool_cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=fake_pool_cm)

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value={"id": 42})

    with (
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        patch("polaris_api.db.pools.get_service_pool", return_value=fake_pool),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60198887777",
            text="好的",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "booked"
    customer = mock_ea.create_appointment.call_args.kwargs["customer"]
    assert customer["first_name"] == "王芳"
    assert customer["last_name"] == "Unknown"


@pytest.mark.asyncio(loop_scope="function")
async def test_confirm_yes_books_multi_token_name_keeps_real_last_name() -> None:
    """F1 control: a multi-token name still books with its real last name."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="Ali Bin Ahmad",
        customer_email="ali@example.com",
        customer_phone="+60123456789",
    )

    fake_pool = MagicMock()
    fake_pool_cm = MagicMock()
    fake_pool_cm.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    fake_pool_cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=fake_pool_cm)

    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value={"id": 42})

    with (
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        patch("polaris_api.db.pools.get_service_pool", return_value=fake_pool),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="ya boleh",
            picker_state=state,
        )

    assert reply is not None
    assert reply.kind == "booked"
    customer = mock_ea.create_appointment.call_args.kwargs["customer"]
    assert customer["first_name"] == "Ali"
    assert customer["last_name"] == "Bin Ahmad"


@pytest.mark.asyncio(loop_scope="function")
async def test_cjk_bare_date_picker_on_jumps_to_ranges() -> None:
    """F2: a CJK bare date ('7月1日') on a picker-ON tenant routes to the picker
    instead of leaking to the agent (the Finding-2 hole, reproduced in Chinese)."""
    conn = _make_conn()
    mock_ea = AsyncMock()
    mock_ea.list_availabilities = AsyncMock(return_value=["09:00", "09:30", "10:00"])
    read_cfg = AsyncMock(return_value={"inchat_picker": True})

    with (
        _settings_patch(),
        patch(
            "polaris_api.services.booking.config.read_booking_config", new=read_cfg
        ),
        _classify_patch("book"),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(7, 3)),
        ),
        _extract_datetime_patch("2026-07-01", None),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="7月1日",
        )

    assert reply is not None
    assert reply.kind == "slot_offer"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_pick"


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("bail_text", ["取消预约", "batal"])
async def test_bail_at_awaiting_pick_clears_multilingual(bail_text: str) -> None:
    """F3: a 中文/Malay whole-message cancel at the ranges step exits the picker."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
    )
    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text=bail_text,
        picker_state=state,
    )
    assert reply is not None
    assert reply.kind == "error"
    assert reply.new_picker_state is None


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("nav_text", ["换日期", "tarikh lain"])
async def test_another_date_at_awaiting_pick_multilingual(nav_text: str) -> None:
    """F3: a 中文/Malay 'another date' at the ranges step re-prompts for the date."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
    )
    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text=nav_text,
        picker_state=state,
    )
    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_date"


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("earliest_text", ["最早", "paling awal"])
async def test_earliest_at_awaiting_pick_multilingual(earliest_text: str) -> None:
    """F3: a 中文/Malay 'earliest' at the ranges step takes the first offered slot."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
        offset_days=0,
    )
    reply = await handle_booking_command(
        conn,
        tenant_id=_TENANT_ID,
        conversation_id=_CONV_ID,
        channel="whatsapp",
        customer_handle="60123456789",
        text=earliest_text,
        picker_state=state,
    )
    assert reply is not None
    assert reply.kind == "slot_confirm"
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.step == "awaiting_details"
    assert reply.new_picker_state.resolved_slot == "2026-06-24T09:00:00"


# ---------------------------------------------------------------------------
# Reply localization (en / ms / zh-cn) — static i18n table
# ---------------------------------------------------------------------------


def test_i18n_registry_full_coverage() -> None:
    """Every message key and table covers all 3 languages (no gaps → no en fallback)."""
    from polaris_api.services.booking import _i18n

    for key, row in _i18n.T.items():
        assert set(row.keys()) == {"en", "ms", "zh-cn"}, key
    for lang in _i18n.LANGS:
        assert len(_i18n.WEEKDAYS[lang]) == 7
        assert len(_i18n.MONTHS[lang]) == 13  # index 0 unused
        assert len(_i18n.AMPM[lang]) == 2
        assert lang in _i18n.AND


def test_fmt_12h_localized() -> None:
    from polaris_api.services.booking.conversational import _fmt_12h

    assert _fmt_12h(9, 0) == "9:00 AM"  # en unchanged
    assert _fmt_12h(14, 0) == "2:00 PM"
    assert _fmt_12h(9, 0, "ms") == "9:00 pagi"
    assert _fmt_12h(14, 0, "ms") == "2:00 petang"
    assert _fmt_12h(9, 0, "zh-cn") == "上午9:00"
    assert _fmt_12h(14, 0, "zh-cn") == "下午2:00"


def test_format_iso_slot_localized() -> None:
    from polaris_api.services.booking.conversational import _format_iso_slot

    iso = "2026-07-01T09:00:00+08:00"  # Wednesday
    assert _format_iso_slot(iso) == "Wed 1 Jul at 9:00 AM"  # en unchanged
    assert _format_iso_slot(iso, "ms") == "Rab, 1 Jul 9:00 pagi"
    assert _format_iso_slot(iso, "zh-cn") == "周三 7月1日 上午9:00"


def test_format_date_label_localized() -> None:
    from polaris_api.services.booking.conversational import _format_date_label

    d = date(2026, 7, 1)  # Wednesday
    assert _format_date_label(d) == "Wed, 1 Jul"  # en unchanged
    assert _format_date_label(d, "ms") == "Rab, 1 Jul"
    assert _format_date_label(d, "zh-cn") == "周三 7月1日"


def test_render_range_offer_localized() -> None:
    from polaris_api.services.booking.conversational import _render_range_offer

    slots = ["2026-07-01T09:00:00+08:00", "2026-07-01T09:30:00+08:00"]
    en = _render_range_offer(slots)
    assert en.startswith("On Wed, 1 Jul we're open") and "What time works for you?" in en
    zh = _render_range_offer(slots, "zh-cn")
    assert "营业时间" in zh and "您想约几点" in zh
    ms = _render_range_offer(slots, "ms")
    assert "kami buka" in ms and "Pukul berapa" in ms


@pytest.mark.asyncio(loop_scope="function")
async def test_confirm_yes_books_renders_chinese() -> None:
    """A zh-cn picker state books and the confirmation is in Chinese."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot="2026-06-24T18:00:00",
        customer_name="王芳",
        customer_email="wangfang@example.com",
        customer_phone="+60198887777",
        language="zh-cn",
    )
    fake_pool = MagicMock()
    fake_pool_cm = MagicMock()
    fake_pool_cm.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    fake_pool_cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=fake_pool_cm)
    mock_ea = AsyncMock()
    mock_ea.create_appointment = AsyncMock(return_value={"id": 42})
    with (
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
        patch("polaris_api.db.pools.get_service_pool", return_value=fake_pool),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60198887777",
            text="好的",
            picker_state=state,
        )
    assert reply is not None
    assert reply.kind == "booked"
    assert "预约成功" in reply.content
    assert "王芳" in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_no_time_reprompt_renders_malay() -> None:
    """A ms picker state with no understood time re-shows ranges in Malay."""
    conn = _make_picker_conn()
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=3,
        provider_id=7,
        offered_slots=_OFFERED_SLOTS,
        resolved_slot=None,
        language="ms",
    )
    with _extract_datetime_patch(None, None):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="hmm",
            picker_state=state,
        )
    assert reply is not None
    assert reply.kind == "slot_offer"
    assert "Saya tak pasti masanya" in reply.content
    assert "kami buka" in reply.content


@pytest.mark.asyncio(loop_scope="function")
async def test_bare_date_entry_persists_detected_language() -> None:
    """Entering the picker with a CJK date persists language='zh-cn' on the state."""
    conn = _make_conn()
    mock_ea = AsyncMock()
    mock_ea.list_availabilities = AsyncMock(return_value=["09:00", "09:30"])
    read_cfg = AsyncMock(return_value={"inchat_picker": True})
    with (
        _settings_patch(),
        patch("polaris_api.services.booking.config.read_booking_config", new=read_cfg),
        _classify_patch("book"),
        patch(
            "polaris_api.services.booking.conversational.branch_config"
            ".get_branch_ea_config",
            new=AsyncMock(return_value=(7, 3)),
        ),
        _extract_datetime_patch("2026-07-01", None),
        _ea_calendar_patch({"timezone": "Asia/Kuala_Lumpur"}),
        patch(
            "polaris_api.services.booking.client.get_ea_booking_client",
            return_value=mock_ea,
        ),
    ):
        reply = await handle_booking_command(
            conn,
            tenant_id=_TENANT_ID,
            conversation_id=_CONV_ID,
            channel="whatsapp",
            customer_handle="60123456789",
            text="7月1日",
        )
    assert reply is not None
    assert reply.new_picker_state is not None
    assert reply.new_picker_state.language == "zh-cn"
    # And the ranges are rendered in Chinese.
    assert "您想约几点" in reply.content


def test_picker_state_language_roundtrip_and_legacy_default() -> None:
    """language round-trips through to_dict/from_dict; legacy blobs default to en."""
    state = BookingPickerState(step="awaiting_pick", language="ms")
    assert BookingPickerState.from_dict(state.to_dict()).language == "ms"
    # Legacy Redis blob without the key → "en".
    legacy = {"step": "awaiting_pick", "service_id": 3}
    assert BookingPickerState.from_dict(legacy).language == "en"
    # Garbage language coerces to en.
    assert BookingPickerState.from_dict({"language": "fr"}).language == "en"


def test_detect_booking_language_malay_boost() -> None:
    """Terse Malay booking openers (no 2+ stopwords) still detect as 'ms'."""
    from polaris_api.services.booking.conversational import _detect_booking_language

    for ms_text in ["5 ogos", "nak book", "esok", "isnin depan", "pukul 3 petang",
                    "nak tempah", "1 mei", "3 mac"]:
        assert _detect_booking_language(ms_text) == "ms", ms_text
    # English openers stay en; CJK stays zh-cn.
    for en_text in ["1 july", "next monday", "tomorrow morning", "book a table"]:
        assert _detect_booking_language(en_text) == "en", en_text
    assert _detect_booking_language("7月1日") == "zh-cn"
