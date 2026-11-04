"""Conversational cancel / reschedule / new-booking from an inbound chat message (v2.2).

The booking reminder (``services/booking_reminders._build_reminder_text``)
tells the customer they can reply **CONFIRM**, **RESCHEDULE**, or
**CANCEL**. This module turns those replies (and free-text variants in
EN / BM / ZH) into real actions, wired into the inbound pipeline BEFORE
the flow runtime so the behaviour is deterministic and does not depend on
the tenant having authored a flow.

Design (per operator decision 2026-05-31 + 2026-06-24):

  * **CANCEL** — handled directly inside WhatsApp. We cancel the EA
    appointment and flip the Polaris row, then confirm in chat.
  * **RESCHEDULE** — handed off to a signed EA web link (link mode). We
    release the current slot and hand the customer a fresh bound link so
    the new booking is tied back to their verified chat identity (and so
    a later cancel/reschedule can find it). Interactive in-chat slot
    picking remains out of scope.
  * **CONFIRM** — a friendly acknowledgement; no EA mutation (the booking
    is already confirmed).
  * **BOOK (new)** — link mode (Phase 1/1b) or in-chat slot picker (Phase 2
    when ``booking_config.inchat_picker`` is true). Operates WITHOUT a prior
    booking — the existing-booking guard is bypassed. Resolves
    ``(provider_id, service_id)`` from branch config with EA-list fallback.

Safety:
  * Intent detection for cancel/reschedule/confirm is **gated on the
    customer actually owning a confirmed upcoming booking**
    (``find_customer_bookings``). A message that merely mentions "cancel"
    with no booking on file falls through to the normal LLM/flow path —
    we never act on a booking that isn't there.
  * New-booking intent does NOT require a prior booking, but is gated on
    the channel being bookable (``_BOOKABLE_CHANNELS``).
  * All lookups/mutations key on the VERIFIED ``customer_handle`` (the
    signed-webhook channel_user_id), never on anything the customer
    types — the impersonation guard in ``cancel_reschedule`` is reused
    verbatim.
  * We operate on the **soonest** upcoming booking and name it explicitly
    in the reply, so a customer with multiple bookings sees exactly what
    changed and can repeat the command for the next one. (Stateless — no
    multi-turn disambiguation at this stage.)

Phase 2 in-chat picker state is managed by the pipeline
(``_load_booking_picker_state`` / ``_save_booking_picker_state``) and
routed here via the ``picker_state`` parameter. The picker state lives in
Redis under the ``"book:"`` prefix with a 30-minute TTL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import asyncpg

# NOTE: get_settings / branch_config / link_flow / ensure_binding_configured /
# cancel_booking / find_customer_bookings / cancel_booking_reminders below are
# not called directly in THIS file — they're accessed as `conversational.<name>`
# from formatting.py / cancel_reschedule_flow.py / picker_statemachine.py (see
# the import-block comment further down) so that
# `tests/booking/test_conversational.py`'s `unittest.mock.patch(
# "polaris_api.services.booking.conversational.<name>")` monkeypatches keep
# intercepting the call. Keep them imported here even though ruff can't see
# the cross-module attribute access.
from polaris_api.config import get_settings  # noqa: F401
from polaris_api.services.booking import branch_config, link_flow  # noqa: F401
from polaris_api.services.booking import nlu as booking_nlu
from polaris_api.services.booking.binding_token import (
    ensure_configured as ensure_binding_configured,  # noqa: F401
)
from polaris_api.services.booking.cancel_reschedule import (
    cancel_booking,  # noqa: F401
    find_customer_bookings,  # noqa: F401
)
from polaris_api.services.booking.slot_picker_state import BookingPickerState
from polaris_api.services.booking_reminders import cancel_booking_reminders  # noqa: F401

logger = logging.getLogger(__name__)

_BOOKABLE_CHANNELS = frozenset({"whatsapp", "messenger", "instagram_login", "telegram", "email"})


BookingReplyKind = Literal[
    "cancelled",
    "reschedule_link",
    "reschedule_page",
    "confirmed",
    "error",
    "book_link",
    "book_page",
    "slot_offer",
    "slot_confirm",
    "booked",
]


@dataclass
class BookingReply:
    """The bot's reply to a booking command. ``content`` is sent to the
    customer; ``kind`` is surfaced for logging / telemetry.

    ``new_picker_state`` (Phase 2) carries the updated
    :class:`~polaris_api.services.booking.slot_picker_state.BookingPickerState`
    the pipeline persists after handling the reply; ``None`` clears any active
    picker state. The pipeline saves it unconditionally, so the reply itself is
    the single source of truth for picker-state lifecycle.
    """

    content: str
    kind: BookingReplyKind
    new_picker_state: BookingPickerState | None = None


# The 4 modules below were extracted from this file in Batch E (task E2; see
# PRP plan audit-batch-e-structural-splits). This module remains the facade:
# `handle_booking_command` is the one real entry point the pipeline calls,
# `BookingReply`/`BookingReplyKind` (above) are the shared return type every
# sub-module produces, and the imports below both (a) supply the names
# `handle_booking_command` itself calls and (b) re-export every name the
# existing test suite (`tests/booking/test_conversational.py`) still imports
# directly from `polaris_api.services.booking.conversational`. The sub-modules
# access `branch_config`, `link_flow`, `ensure_binding_configured`,
# `find_customer_bookings`, `cancel_booking`, `cancel_booking_reminders`,
# `get_settings`, `_resolve_provider_service`, and `BookingReply` via
# `conversational.<name>` (this module, imported by reference) rather than
# importing them directly, so the test suite's `conversational.<name>`
# monkeypatches keep intercepting the call regardless of which module now
# owns the calling code. This creates an intentional two-way import at the
# MODULE level (conversational -> {4 submodules} -> conversational); it is
# safe because the submodules only bind `conversational` as a module
# reference (never `from .conversational import <name>`), so they never need
# conversational.py to have finished executing — only that the specific
# attribute they access at CALL time exists by then (which it does, since
# BookingReply/BookingReplyKind are defined above this import block).
from polaris_api.services.booking.cancel_reschedule_flow import (
    _do_cancel,
    _do_reschedule,
    _resolve_provider_service,  # noqa: F401  (re-export — tests patch conversational._resolve_provider_service)
)
from polaris_api.services.booking.formatting import (
    _ask_for_details,  # noqa: F401  (re-export — imported directly by test_conversational.py)
    _compress_ranges,  # noqa: F401  (re-export)
    _fmt_12h,  # noqa: F401  (re-export)
    _format_date_label,  # noqa: F401  (re-export)
    _format_iso_slot,  # noqa: F401  (re-export)
    _format_slot,
    _match_time,  # noqa: F401  (re-export)
    _normalize_phone,  # noqa: F401  (re-export)
    _pretty_time,  # noqa: F401  (re-export)
    _render_range_offer,  # noqa: F401  (re-export)
    _resolve_tenant_timezone,
    _slot_minutes,  # noqa: F401  (re-export)
    _split_name,  # noqa: F401  (re-export)
)
from polaris_api.services.booking.intent_detect import (
    _detect_booking_language,
    _inchat_picker_on,
    _looks_booking_ish,
    _looks_date_ish,
    detect_booking_command,
)
from polaris_api.services.booking.picker_statemachine import (
    _WEEKDAY_KEYS,  # noqa: F401  (re-export — imported directly by test_conversational.py)
    _do_new_booking,
    _handle_picker_turn,
)

# Explicit re-export list — mypy strict mode requires this for the
# sub-modules' `conversational.<name>` attribute access (module-reference
# pattern, see the comment above) to type-check as an intentional public
# surface rather than an implicit re-export. Also documents, in one place,
# every name `tests/booking/test_conversational.py` imports directly from or
# monkeypatches on this module.
__all__ = [
    "BookingReply",
    "BookingReplyKind",
    "cancel_booking",
    "cancel_booking_reminders",
    "detect_booking_command",
    "ensure_binding_configured",
    "find_customer_bookings",
    "get_settings",
    "handle_booking_command",
    "branch_config",
    "link_flow",
    "_ask_for_details",
    "_compress_ranges",
    "_detect_booking_language",
    "_do_cancel",
    "_do_new_booking",
    "_do_reschedule",
    "_fmt_12h",
    "_format_date_label",
    "_format_iso_slot",
    "_format_slot",
    "_handle_picker_turn",
    "_inchat_picker_on",
    "_looks_booking_ish",
    "_looks_date_ish",
    "_match_time",
    "_normalize_phone",
    "_pretty_time",
    "_render_range_offer",
    "_resolve_provider_service",
    "_resolve_tenant_timezone",
    "_slot_minutes",
    "_split_name",
    "_WEEKDAY_KEYS",
]


async def handle_booking_command(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    text: str,
    picker_state: BookingPickerState | None = None,
    chatwoot: object | None = None,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
) -> BookingReply | None:
    """Detect and act on a cancel/reschedule/confirm/book command.

    Returns a :class:`BookingReply` when the message was a booking command
    that we handled. Returns ``None`` (fall through to the normal pipeline)
    when the message is not a booking command, or when the customer has no
    booking to act on for cancel/reschedule/confirm.

    For the **``book``** command, no prior booking is required — we branch
    BEFORE the ``find_customer_bookings`` gate.

    ``picker_state`` (Phase 2) carries an active in-chat slot-picking
    conversation. When non-None and the channel supports in-chat picking,
    the picker flow intercepts the message before any intent detection.

    ``conn`` is the pipeline's RLS-scoped connection (``app.current_
    tenant_id`` GUC already set), so all reads/writes here stay tenant-
    scoped. EA HTTP calls happen inline (same posture as the booking
    flow node).
    """
    # Phase 2: if the pipeline has loaded an active picker state for this
    # conversation, route into the picker flow BEFORE intent detection so
    # the agent never steals the customer's "yes"/slot message.
    if picker_state is not None:
        return await _handle_picker_turn(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            text=text,
            state=picker_state,
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
        )

    command = detect_booking_command(text)

    # Hybrid detector — LLM edge-case fallback. The phrase table above handles the
    # bulk; when it misses on a bookable channel, classify with Haiku IF there's a
    # cue:
    #   * a booking keyword (any bookable tenant) — natural phrasings the table
    #     can't enumerate ("i'd like a booking"); OR
    #   * a concrete date / availability cue ("1 july", "next monday", "are you
    #     free?") — but ONLY when the tenant has the in-chat picker on, so a bare
    #     date is never mis-routed for a non-picker tenant.
    # This is the structural fix for the agent inventing availability: a
    # date/availability message now enters the deterministic picker instead of
    # falling through to the agent. Gated so normal traffic spends no LLM call.
    # ``picker_on`` is resolved here ONLY for the date-ish-only gate, then reused by
    # _do_new_booking below (avoids a second identical booking_config read).
    picker_on: bool | None = None
    if command is None and channel in _BOOKABLE_CHANNELS:
        keyworded = _looks_booking_ish(text)
        if keyworded:
            pass  # keyword path: classify regardless of picker (unchanged)
        elif _looks_date_ish(text):
            picker_on = await _inchat_picker_on(conn, tenant_id)
        if keyworded or picker_on:
            command = await booking_nlu.classify_booking_intent(text)
            if command is not None:
                logger.info(
                    "booking_command: LLM fallback resolved intent=%s tenant=%s",
                    command,
                    tenant_id,
                )

    if command is None:
        return None

    if channel not in _BOOKABLE_CHANNELS:
        # Booking commands only make sense on bookable channels.
        return None

    # NEW-BOOKING branch: does NOT require an existing booking.
    # Place BEFORE the find_customer_bookings gate so an empty booking
    # history doesn't swallow the "book" intent.
    if command == "book":
        # A concrete date in the message → jump straight to that date's real ranges
        # (the picker), skipping the "what date?" prompt; otherwise the normal offer.
        language = _detect_booking_language(text)
        return await _do_new_booking(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
            prefetch_date_text=text if _looks_date_ish(text) else None,
            inchat_picker=picker_on,  # reuse the gate's read when it ran
            language=language,
        )

    bookings = await find_customer_bookings(
        conn, tenant_id=tenant_id, channel=channel, customer_handle=customer_handle
    )
    if not bookings:
        # No booking on file → not our command; let the LLM/flow answer.
        logger.info(
            "booking_command: %s with no upcoming booking tenant=%s — fall through",
            command,
            tenant_id,
        )
        return None

    target = bookings[0]  # soonest upcoming
    tenant_timezone = await _resolve_tenant_timezone(conn, tenant_id)

    if command == "confirm":
        return BookingReply(
            content=(
                f"You're all set for {_format_slot(target, tenant_timezone)}. "
                "See you then! Reply *RESCHEDULE* to change the time or "
                "*CANCEL* to cancel."
            ),
            kind="confirmed",
        )

    if command == "cancel":
        return await _do_cancel(
            conn,
            tenant_id=tenant_id,
            customer_handle=customer_handle,
            target=target,
            tenant_timezone=tenant_timezone,
        )

    # command == "reschedule"
    return await _do_reschedule(
        conn,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        customer_handle=customer_handle,
        target=target,
        tenant_timezone=tenant_timezone,
    )
