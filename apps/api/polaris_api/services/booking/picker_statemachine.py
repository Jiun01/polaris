"""In-chat slot-picker state machine — extracted from conversational.py
(Batch E, task E2; see PRP plan audit-batch-e-structural-splits).

Phase 2 in-chat picker: ``_do_new_booking`` (new-booking entry, also used by
the "book" intent), ``_handle_picker_turn`` (the per-turn dispatch hub) and
its ``_picker_awaiting_*`` state handlers, plus the picker's own detail/edit
helpers.

Calls back into ``formatting.py`` for rendering and into
``cancel_reschedule_flow._resolve_provider_service`` (via
``conversational._resolve_provider_service`` — see that module's docstring)
for provider/service resolution.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from polaris_api.services.booking import _i18n, conversational
from polaris_api.services.booking import nlu as booking_nlu
from polaris_api.services.booking.client import public_booking_url
from polaris_api.services.booking.formatting import (
    _DEFAULT_SLOT_MINUTES,
    _EMAIL_RE,
    _PHONE_RE,
    _ask_for_details,
    _extract_email,
    _extract_phone,
    _format_date_label,
    _format_iso_slot,
    _hours_to_iso,
    _match_time,
    _mint_booking_link,
    _mint_short_link,
    _missing_labels,
    _normalize_phone,
    _pretty_time,
    _render_range_offer,
    _render_summary_reply,
    _split_name,
)
from polaris_api.services.booking.slot_picker_state import BookingPickerState

logger = logging.getLogger(__name__)


async def _do_new_booking(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    chatwoot: object | None = None,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
    prefetch_date_text: str | None = None,
    inchat_picker: bool | None = None,
    language: str = "en",
) -> conversational.BookingReply:
    """Handle a new-booking intent (link mode, Phase 1).

    Mirrors ``_do_reschedule`` minus the slot-release step. Builds a
    bound EA wizard link (``kind="book_link"``) or falls back to the bare
    public page (``kind="book_page"``) when provider/service/binding is
    unavailable — never strands the customer.

    Phase 2 (in-chat picker): if ``booking_config.inchat_picker`` is true
    AND both provider/service are resolvable, offers numbered slots instead
    of a link.  inchat_picker defaults to false so Phase 2 ships dark.
    """
    settings = conversational.get_settings()

    slug = await conn.fetchval("SELECT slug FROM tenants WHERE id = $1", tenant_id)
    if not isinstance(slug, str) or not slug:
        logger.warning("booking_command new: tenant has no slug tenant=%s", tenant_id)
        return conversational.BookingReply(
            content="Sorry, I couldn't start a booking right now. Reply *HELP* to reach our team.",
            kind="error",
        )

    try:
        base_url = public_booking_url(base_url=settings.ea_booking_public_url, tenant_slug=slug)
    except ValueError:
        logger.warning("booking_command new: invalid base_url/slug tenant=%s", tenant_id)
        return conversational.BookingReply(
            content="Sorry, I couldn't start a booking right now. Reply *HELP* to reach our team.",
            kind="error",
        )

    provider_id, service_id = await conversational._resolve_provider_service(
        tenant_id=tenant_id, conversation_id=conversation_id
    )

    # Check Phase 2 opt-in. Reuse the flag the caller's gate already read (the
    # bare-date path), else read it once here — avoids a second identical query.
    if inchat_picker is None:
        inchat_picker = False
        try:
            from polaris_api.services.booking.config import read_booking_config

            booking_config = await read_booking_config(conn, tenant_id)
            inchat_picker = bool(booking_config.get("inchat_picker", False))
        except Exception:  # noqa: BLE001 — fail-soft to link mode
            logger.warning(
                "booking_command new: booking_config read failed tenant=%s",
                tenant_id,
                exc_info=True,
            )

    # A named date couldn't reach the picker (provider/service unresolved) → it
    # falls to link mode below; note it so a misconfigured tenant is diagnosable.
    if prefetch_date_text is not None and (
        not inchat_picker or provider_id is None or service_id is None
    ):
        logger.info(
            "booking_command new: prefetch date dropped to link mode "
            "(inchat_picker=%s provider=%s service=%s) tenant=%s",
            inchat_picker,
            provider_id,
            service_id,
            tenant_id,
        )

    # Phase 2: in-chat slot picker (dark by default).
    if inchat_picker and provider_id is not None and service_id is not None:
        # The customer already named a date ("1 july") → bind a fresh awaiting_date
        # state and hand it straight to _picker_awaiting_date, so they see that
        # date's REAL ranges immediately (no "what date?" round-trip, no link mint).
        if prefetch_date_text is not None:
            state = BookingPickerState(
                step="awaiting_date",
                service_id=service_id,
                provider_id=provider_id,
                language=language,
            )
            return await _picker_awaiting_date(
                conn,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                customer_handle=customer_handle,
                text=prefetch_date_text,
                state=state,
                chatwoot=chatwoot,
                cw_account_id=cw_account_id,
                cw_conv_id=cw_conv_id,
            )
        return await _do_inchat_offer(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            provider_id=provider_id,
            service_id=service_id,
            language=language,
        )

    # --- Link mode (Phase 1) ---
    return await _mint_booking_link(
        conn,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        customer_handle=customer_handle,
        provider_id=provider_id,
        service_id=service_id,
        base_url=base_url,
    )


_SLOT_WINDOW_DAYS = 14


_WEEKDAY_KEYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


_ANOTHER_DATE_PHRASES = frozenset(
    {
        "another date",
        "different date",
        "other date",
        "another day",
        "different day",
        "other day",
        "change date",
        "change the date",
        "more",
        "next",
        "later",
        # Bahasa Melayu
        "tarikh lain",
        "hari lain",
        "tukar tarikh",
        # 中文
        "换日期",
        "換日期",
        "其他日期",
        "改天",
        "改日",
        "别的日期",
        "其他时间",
    }
)


def _ask_for_date_again(state: BookingPickerState) -> conversational.BookingReply:
    """Reset to the ``awaiting_date`` prompt, keeping the bound service/provider."""
    new_state = replace(
        state, step="awaiting_date", offered_slots=[], resolved_slot=None, offset_days=0
    )
    return conversational.BookingReply(
        content=_i18n.t("ask_date_again", state.language),
        kind="slot_offer",
        new_picker_state=new_state,
    )


def _picker_bail(*, booked_context: bool = False, lang: str = "en") -> conversational.BookingReply:
    """Exit the picker entirely — shared by every step's bail-phrase check.

    ``booked_context`` (the final-confirm step) reassures the customer nothing was
    booked; every step clears the picker (``new_picker_state=None``).
    """
    content = _i18n.t("bail_not_booked", lang) if booked_context else _i18n.t("bail_generic", lang)
    return conversational.BookingReply(content=content, kind="error", new_picker_state=None)


async def _resolve_service_duration_minutes(
    client: object, *, tenant_id: UUID, service_id: int
) -> int:
    """Return the EA service's duration in minutes (``_DEFAULT_SLOT_MINUTES``
    on any miss/error).

    The offered slot came from duration-aware availability, and EA honours the
    ``end_datetime`` we pass (it only auto-derives when omitted) — so booking
    with the wrong length would mis-size the appointment and can block the
    adjacent slot. Read the real duration off the service record.
    """
    try:
        services = await client.list_services(tenant_id=tenant_id)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — fail-soft to the default length
        return _DEFAULT_SLOT_MINUTES
    for svc in services:
        if isinstance(svc, dict) and svc.get("id") == service_id:
            dur = svc.get("duration")
            if isinstance(dur, (int, float)) and dur > 0:
                return int(dur)
    return _DEFAULT_SLOT_MINUTES


async def _do_inchat_offer(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    provider_id: int,
    service_id: int,
    language: str = "en",
) -> conversational.BookingReply:
    """Date-first in-chat booking offer (Phase 2 offer step).

    Asks the customer for a date AND includes the bound booking link in the same
    message (hybrid), then waits in ``awaiting_date``. The date reply is handled
    by ``_picker_awaiting_date``, which queries EA for that single date (the only
    shape EA's availability endpoint supports) and offers real local-time slots.

    The booking link is minted up front so a customer who would rather tap
    through than pick a date in chat can still do so. The updated picker state
    rides back on ``conversational.BookingReply.new_picker_state``; the pipeline persists it.
    """
    short_url = await _mint_short_link(
        conn,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        customer_handle=customer_handle,
        provider_id=provider_id,
        service_id=service_id,
    )

    content = _i18n.t("offer_date_prompt", language)
    if short_url is not None:
        content = content + _i18n.t("offer_link_tail", language, url=short_url)

    new_state = BookingPickerState(
        step="awaiting_date",
        service_id=service_id,
        provider_id=provider_id,
        offered_slots=[],  # filled once the customer names a date
        resolved_slot=None,
        offset_days=0,
        language=language,
    )
    return conversational.BookingReply(
        content=content,
        kind="slot_offer",
        new_picker_state=new_state,
    )


async def _resolve_calendar(
    conn: asyncpg.Connection, tenant_id: UUID
) -> tuple[str, dict[str, object] | None]:
    """Read the EA calendar mirror once → ``(provider_tz, working_plan)``.

    One ``tenants.settings`` read serves both the date turn (needs the tz) and the
    empty-date reply (needs the working_plan), instead of two fetches on the same
    turn.

    * ``provider_tz``: ``ea_calendar.timezone`` → ``booking_config.tenant_timezone``
      → ``"UTC"``. Both keys mirror EA's ``default_timezone`` so they agree in
      practice; the booking_config read is only a fallback for a not-yet-synced
      mirror (so it's the rare second read, not the common path).
    * ``working_plan``: ``ea_calendar.working_plan`` (weekday → day-config / null)
      or ``None`` when unavailable (the empty-date path then defaults to the safe
      "closed" wording).

    Fail-soft: any read error yields ``("UTC", None)`` so a config hiccup only
    mis-localizes display, never breaks the offer.
    """
    try:
        from polaris_api.services.booking.config import (
            read_booking_config,
            read_ea_calendar,
        )

        calendar = await read_ea_calendar(conn, tenant_id)
        plan = calendar.get("working_plan")
        working_plan = plan if isinstance(plan, dict) else None
        tz = calendar.get("timezone")
        if isinstance(tz, str) and tz:
            return tz, working_plan
        booking_config = await read_booking_config(conn, tenant_id)
        tz = booking_config.get("tenant_timezone")
        if isinstance(tz, str) and tz:
            return tz, working_plan
        return "UTC", working_plan
    except Exception:  # noqa: BLE001 — calendar lookup is best-effort
        logger.warning(
            "booking_command picker: calendar read failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return "UTC", None


async def _handle_picker_turn(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    text: str,
    state: BookingPickerState,
    chatwoot: object | None = None,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
) -> conversational.BookingReply | None:
    """Handle an in-progress in-chat slot-picker turn (Phase 2).

    Routes based on ``state.step``:
      * ``awaiting_date`` — parse the customer's date, fetch that date's EA
        availability, and offer local-time slots (or fully-booked/closed +
        nearest opening).
      * ``awaiting_pick`` — match the time the customer names against the full
        day's slots (exact or nearest); route "another date" back to
        ``awaiting_date``; re-show the open ranges on no match.
      * ``awaiting_details`` — collect Name + Email + Phone (regex for email/
        phone, LLM for name), prefilling from Chatwoot; loop until complete.
      * ``awaiting_confirm`` — final summary; book on a free-flow yes, re-resolve
        on an edit (new time or any detail), re-ask otherwise.

    ``chatwoot`` + ``cw_account_id`` + ``cw_conv_id`` (optional) let the details
    step prefill phone/email from the Chatwoot contact; absent → ask for all.

    Returns ``None`` only when the state is invalid/unexpected (fall through).
    """
    # "cancel" / "stop" / "nevermind" exits the picker from ANY step — checked
    # once here so the per-step handlers only deal with their own in-band input.
    if text.strip().lower() in _PICKER_BAIL_PHRASES:
        return _picker_bail(booked_context=state.step == "awaiting_confirm", lang=state.language)

    if state.step == "awaiting_date":
        return await _picker_awaiting_date(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            text=text,
            state=state,
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
        )
    if state.step == "awaiting_pick":
        return await _picker_awaiting_pick(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            text=text,
            state=state,
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
        )
    if state.step == "awaiting_details":
        return await _picker_awaiting_details(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            text=text,
            state=state,
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
        )
    if state.step == "awaiting_confirm":
        return await _picker_awaiting_confirm(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            text=text,
            state=state,
        )
    # Unknown step — clear state by returning a fresh offer.
    logger.warning(
        "booking_command picker: unknown step %r tenant=%s — resetting",
        state.step,
        tenant_id,
    )
    return None


_PICKER_BAIL_PHRASES = frozenset(
    {
        "cancel",
        "stop",
        "nevermind",
        "never mind",
        "no thanks",
        "no thank you",
        # Bahasa Melayu
        "batal",
        "batalkan",
        "berhenti",
        "tak jadi",
        "lupakan",
        # 中文
        "取消",
        "取消预约",
        "取消預約",
        "停止",
        "算了",
        "不用了",
        "不要了",
    }
)


_EARLIEST_PHRASES = frozenset(
    {
        "earliest",
        "soonest",
        "first",
        "first one",
        "first available",
        "asap",
        "anytime",
        # Bahasa Melayu
        "paling awal",
        "awal sekali",
        "seawal mungkin",
        # 中文
        "最早",
        "越早越好",
        "最早的",
        "都可以",
        "随便",
    }
)


_TIME_HINT_RE = re.compile(
    r"\d{1,2}\s*[:.]\s*\d{2}|\d{1,2}\s*[ap]\.?m\.?|noon|midday|midnight",
    re.IGNORECASE,
)


_NAME_HINT_RE = re.compile(r"\b(?:name is|name's|i am|i'm|call me|this is)\b", re.IGNORECASE)


_MAX_DETAIL_ATTEMPTS = 4


def _missing_detail_fields(state: BookingPickerState) -> list[str]:
    """Which of name / email / phone are still unset (email + phone required)."""
    missing = []
    if not state.customer_name:
        missing.append("name")
    if not state.customer_email:
        missing.append("email")
    if not state.customer_phone:
        missing.append("phone")
    return missing


async def _fetch_contact_prefill(
    chatwoot: object | None, cw_account_id: int | None, cw_conv_id: int | None
) -> dict[str, str]:
    """Best-effort Chatwoot contact lookup → ``{name?,email?,phone?}`` (or ``{}``)."""
    if chatwoot is None or cw_account_id is None or cw_conv_id is None:
        return {}
    try:
        result: dict[str, str] = await chatwoot.get_contact_details(  # type: ignore[attr-defined]
            account_id=cw_account_id, conversation_id=cw_conv_id
        )
        return result
    except Exception:  # noqa: BLE001 — prefill is best-effort
        logger.warning("booking_command picker: contact prefill failed", exc_info=True)
        return {}


async def _begin_details(
    state: BookingPickerState,
    slot_iso: str,
    *,
    exact: bool,
    requested_hhmm: str | None,
    chatwoot: object | None,
    cw_account_id: int | None,
    cw_conv_id: int | None,
    lead: str | None = None,
) -> conversational.BookingReply:
    """Soft-echo the chosen time, prefill from Chatwoot ONCE, then ask for the
    still-missing details. Moves to ``awaiting_details`` (or straight to the final
    summary if everything is already known).

    ``lead`` overrides the soft-echo opener — used by the empty-date path to
    explain the substitution ("Sorry, 26 Jun is fully booked. The nearest opening
    is …") in one place instead of echoing the slot twice. ``resolved_slot`` keeps
    the provider-tz ISO so ``_do_inchat_book`` books the right instant (no skew).
    Name is always asked (never prefilled); phone/email come from the Chatwoot
    contact when present.
    """
    lang = state.language
    pretty = _format_iso_slot(slot_iso, lang)
    if lead is not None:
        echo = lead
    elif exact or requested_hhmm is None:
        echo = _i18n.t("slot_echo", lang, pretty=pretty)
    else:
        echo = _i18n.t(
            "slot_echo_nearest",
            lang,
            requested=_pretty_time(requested_hhmm, lang),
            pretty=pretty,
        )

    prefill = await _fetch_contact_prefill(chatwoot, cw_account_id, cw_conv_id)
    raw_phone = prefill.get("phone")
    new_state = replace(
        state,
        step="awaiting_details",
        resolved_slot=slot_iso,
        customer_email=prefill.get("email") or None,
        # Normalise the Chatwoot phone the same way as a customer-typed one, so a
        # contact stored in an odd format is validated (and dropped → asked) rather
        # than booked into EA verbatim.
        customer_phone=_normalize_phone(raw_phone) if raw_phone else None,
        customer_name=None,  # always ask the name
        detail_attempts=0,
    )

    missing = _missing_detail_fields(new_state)
    if not missing:  # defensive — name is always missing in practice
        return _render_summary_reply(new_state, lead=echo)
    return conversational.BookingReply(
        content=_i18n.t("almost_there", lang, echo=echo, ask=_ask_for_details(missing, lang)),
        kind="slot_confirm",
        new_picker_state=new_state,
    )


async def _apply_contact_fields(state: BookingPickerState, text: str) -> BookingPickerState:
    """Merge any name/email/phone found in ``text`` into ``state``.

    Email + phone via regex (deterministic); name via the Haiku helper, and only
    when the name is still missing (so a normal turn spends at most one LLM call).
    Already-set fields are never overwritten here (use the edit path for that).
    """
    new_state = state
    if new_state.customer_email is None and (email := _extract_email(text)):
        new_state = replace(new_state, customer_email=email)
    if new_state.customer_phone is None and (phone := _extract_phone(text)):
        new_state = replace(new_state, customer_phone=phone)
    if new_state.customer_name is None:
        # Only spend a Haiku call when there's leftover text beyond the email/phone
        # — so "John Tan john@x.com" still extracts a name, but a bare email/phone
        # reply doesn't trigger a pointless LLM round-trip.
        leftover = _PHONE_RE.sub(" ", _EMAIL_RE.sub(" ", text)).strip()
        if leftover:
            name = await booking_nlu.extract_customer_name(text)
            if name:
                new_state = replace(new_state, customer_name=name)
    return new_state


async def _apply_edit(state: BookingPickerState, text: str) -> tuple[BookingPickerState, bool]:
    """At the final confirm, interpret a message as an edit (time or any detail).

    Returns ``(new_state, changed)``. Email/phone are matched by regex; a new time
    re-matches the day's slots (gated by a cheap time-hint so a plain "yes" costs
    no LLM call); a name change is gated by a name-hint. Date changes are out of
    scope here — the customer can pick another date earlier in the flow.
    """
    new_state = state
    changed = False

    email = _extract_email(text)
    if email and email != new_state.customer_email:
        new_state = replace(new_state, customer_email=email)
        changed = True

    phone = _extract_phone(text)
    if phone and phone != new_state.customer_phone:
        new_state = replace(new_state, customer_phone=phone)
        changed = True

    if _TIME_HINT_RE.search(text) and new_state.offered_slots:
        _, requested_time = await booking_nlu.extract_booking_datetime(
            text, today_iso=date.today().isoformat(), timezone="UTC"
        )
        if requested_time:
            slot, _exact = _match_time(new_state.offered_slots, requested_time)
            if slot and slot != new_state.resolved_slot:
                new_state = replace(new_state, resolved_slot=slot)
                changed = True

    if _NAME_HINT_RE.search(text):
        name = await booking_nlu.extract_customer_name(text)
        if name and name != new_state.customer_name:
            new_state = replace(new_state, customer_name=name)
            changed = True

    return new_state, changed


async def _picker_awaiting_date(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    text: str,
    state: BookingPickerState,
    chatwoot: object | None = None,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
) -> conversational.BookingReply:
    """Handle a date reply: fetch that date's EA times and offer local slots.

    On a parseable, bookable date with availability we move to ``awaiting_pick``
    with provider-tz ISO slots. A date+time in one message ("30 Jun at 2pm")
    matches the time directly and flows to the details capture. An empty date
    routes to ``_empty_date_reply`` (fully-booked vs closed + nearest opening). An
    unparseable date re-prompts in place.
    """
    from polaris_api.services.booking.client import get_ea_booking_client

    sid, pid = state.service_id, state.provider_id
    if sid is None or pid is None:
        logger.error(
            "booking_command picker: missing service/provider in awaiting_date "
            "tenant=%s — resetting",
            tenant_id,
        )
        return conversational.BookingReply(
            content=_i18n.t("start_over", state.language),
            kind="error",
            new_picker_state=None,
        )

    tz, working_plan = await _resolve_calendar(conn, tenant_id)
    # Resolve "today" in the TENANT's timezone (not the server clock) so a date
    # parsed near midnight isn't wrongly rejected as past / shifted a day.
    try:
        today_iso = datetime.now(ZoneInfo(tz)).date().isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        today_iso = date.today().isoformat()
    chosen_iso, requested_time = await booking_nlu.extract_booking_datetime(
        text, today_iso=today_iso, timezone=tz
    )
    if chosen_iso is None:
        return conversational.BookingReply(
            content=_i18n.t("didnt_catch_date", state.language),
            kind="slot_offer",
            new_picker_state=state,  # stay in awaiting_date
        )
    chosen_date = date.fromisoformat(chosen_iso)

    try:
        client = get_ea_booking_client()
        hours = await client.list_availabilities(
            tenant_id=tenant_id,
            service_id=sid,
            provider_id=pid,
            date_from=chosen_date,
            date_to=chosen_date,  # EA endpoint is single-date (date_to ignored)
        )
    except Exception:  # noqa: BLE001 — fail-soft: treat as empty + suggest nearest
        logger.warning(
            "booking_command picker: EA availability failed tenant=%s date=%s",
            tenant_id,
            chosen_date,
            exc_info=True,
        )
        hours = []

    slots_iso = _hours_to_iso(chosen_date, hours, tz)
    if slots_iso:
        # Persist the FULL day's slots (no cap) so any stated time can match.
        new_state = replace(
            state,
            step="awaiting_pick",
            offered_slots=list(slots_iso),
            resolved_slot=None,
            offset_days=0,
        )
        # A time given in the same message ("30 Jun at 2pm") → match it directly
        # (exact or nearest) and flow to the details capture; otherwise show the
        # open ranges and let the customer name a time (_picker_awaiting_pick).
        if requested_time is not None:
            slot, exact = _match_time(slots_iso, requested_time)
            if slot is not None:
                return await _begin_details(
                    new_state,
                    slot,
                    exact=exact,
                    requested_hhmm=requested_time,
                    chatwoot=chatwoot,
                    cw_account_id=cw_account_id,
                    cw_conv_id=cw_conv_id,
                )
        return conversational.BookingReply(
            content=_render_range_offer(slots_iso, state.language),
            kind="slot_offer",
            new_picker_state=new_state,
        )

    # No times on the chosen date — classify + suggest the nearest opening.
    return await _empty_date_reply(
        conn,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        customer_handle=customer_handle,
        sid=sid,
        pid=pid,
        chosen_date=chosen_date,
        tz=tz,
        working_plan=working_plan,
        state=state,
        chatwoot=chatwoot,
        cw_account_id=cw_account_id,
        cw_conv_id=cw_conv_id,
    )


async def _find_nearest_slot(
    *,
    tenant_id: UUID,
    sid: int,
    pid: int,
    start: date,
    tz: str,
) -> str | None:
    """Scan forward up to ``_SLOT_WINDOW_DAYS`` for the first open slot.

    Returns the first provider-tz ISO slot found, or ``None`` if nothing opens in
    the window (bounds the EA calls on the empty-date path). One EA call per day;
    stops at the first non-empty day. A mid-scan EA error aborts to ``None``
    (caller falls back to the link).
    """
    from polaris_api.services.booking.client import get_ea_booking_client

    client = get_ea_booking_client()
    for offset in range(_SLOT_WINDOW_DAYS):
        day = start + timedelta(days=offset)
        try:
            hours = await client.list_availabilities(
                tenant_id=tenant_id,
                service_id=sid,
                provider_id=pid,
                date_from=day,
                date_to=day,
            )
        except Exception:  # noqa: BLE001 — abort the scan; caller sends a link
            logger.warning(
                "booking_command picker: nearest-slot scan failed tenant=%s day=%s",
                tenant_id,
                day,
                exc_info=True,
            )
            return None
        slots = _hours_to_iso(day, hours, tz)
        if slots:
            return slots[0]
    logger.info(
        "booking_command picker: no opening within %d days tenant=%s from=%s",
        _SLOT_WINDOW_DAYS,
        tenant_id,
        start,
    )
    return None


async def _empty_date_reply(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    sid: int,
    pid: int,
    chosen_date: date,
    tz: str,
    working_plan: dict[str, object] | None,
    state: BookingPickerState,
    chatwoot: object | None = None,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
) -> conversational.BookingReply:
    """Reply for a date with no slots: fully-booked vs closed + nearest opening.

    "Fully booked" when ``working_plan`` marks the weekday as a working day;
    "closed" when it's a non-working day (or the plan is unavailable — the safe
    default). ``working_plan`` is passed in from the caller's single calendar read
    (no extra DB round-trip). Then we flow the nearest opening through the details
    capture (``_begin_details``) — same soft-echo → collect → final-summary path as
    a normal pick, so a "yes" books with the customer's real details (not a
    placeholder) — or hand over the link when nothing opens within the window.
    """
    lang = state.language
    weekday_key = _WEEKDAY_KEYS[chosen_date.weekday()]
    is_working_day = isinstance(working_plan, dict) and isinstance(
        working_plan.get(weekday_key), dict
    )
    label = _format_date_label(chosen_date, lang)
    prefix = (
        _i18n.t("no_avail_fully_booked", lang, label=label)
        if is_working_day
        else _i18n.t("no_avail_closed", lang, label=label)
    )

    nearest_iso = await _find_nearest_slot(
        tenant_id=tenant_id,
        sid=sid,
        pid=pid,
        start=chosen_date + timedelta(days=1),
        tz=tz,
    )
    if nearest_iso is not None:
        # Carry the nearest slot as the (single) offered slot so a later time edit
        # can still re-match, then flow into the details capture. The substitution
        # is explained via _begin_details's `lead` (one mention of the slot, not two).
        return await _begin_details(
            replace(state, offered_slots=[nearest_iso]),
            nearest_iso,
            exact=True,
            requested_hhmm=None,
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
            lead=_i18n.t(
                "nearest_opening_lead",
                lang,
                prefix=prefix,
                pretty=_format_iso_slot(nearest_iso, lang),
            ),
        )

    # Nothing opens in the window — hand over the booking link (or HELP).
    short_url = await _mint_short_link(
        conn,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        customer_handle=customer_handle,
        provider_id=pid,
        service_id=sid,
    )
    tail = (
        _i18n.t("tail_other_dates", lang, url=short_url)
        if short_url is not None
        else _i18n.t("tail_help", lang)
    )
    return conversational.BookingReply(
        content=_i18n.t("no_openings", lang, prefix=prefix, tail=tail),
        kind="book_link",
        new_picker_state=None,  # picker complete — clear state
    )


async def _picker_awaiting_pick(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    text: str,
    state: BookingPickerState,
    chatwoot: object | None = None,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
) -> conversational.BookingReply:
    """Handle a pick-turn message: match a stated time against the day's slots.

    The customer was shown the day's open ranges; here we resolve the time they
    name. We match against the **full** ``offered_slots`` (the old
    ``offered_slots[:5]`` cap silently dead-ended noon/afternoon picks), taking
    the exact slot or the nearest one, then flow to the details capture.
    "earliest" picks the first slot; "another date" returns to the date prompt;
    an unparseable time re-shows the ranges.
    """
    normalized = text.strip().lower()

    # Defensive: a live picker turn must carry the resolved service+provider
    # (we only ever save the state with both set). A missing one means a
    # stale/partial Redis blob — reset cleanly instead of calling EA with None.
    sid, pid = state.service_id, state.provider_id
    if sid is None or pid is None:
        logger.error(
            "booking_command picker: missing service/provider in state tenant=%s — resetting",
            tenant_id,
        )
        return conversational.BookingReply(
            content=_i18n.t("start_over", state.language),
            kind="error",
            new_picker_state=None,
        )

    # "another date" / "more": go back to the date prompt. (The offered slots are
    # for ONE date — EA's availability endpoint is single-date — so paginating a
    # multi-day window isn't meaningful; the customer just names a different day.)
    if normalized in _ANOTHER_DATE_PHRASES:
        return _ask_for_date_again(state)

    # "earliest"/"soonest": take the first open slot of the day.
    if normalized in _EARLIEST_PHRASES and state.offered_slots:
        return await _begin_details(
            state,
            state.offered_slots[0],
            exact=True,
            requested_hhmm=None,
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
        )

    # Extract a stated time and match it against the FULL day's slots (exact or
    # nearest). One cheap Haiku call on a normal turn; _match_time is local. We
    # only need the time, so the date context (tz/today) is nominal here.
    _, requested_time = await booking_nlu.extract_booking_datetime(
        text, today_iso=date.today().isoformat(), timezone="UTC"
    )
    if requested_time is not None and state.offered_slots:
        slot, exact = _match_time(state.offered_slots, requested_time)
        if slot is not None:
            return await _begin_details(
                state,
                slot,
                exact=exact,
                requested_hhmm=requested_time,
                chatwoot=chatwoot,
                cw_account_id=cw_account_id,
                cw_conv_id=cw_conv_id,
            )

    # No time understood — re-show the open ranges, stay in awaiting_pick.
    return conversational.BookingReply(
        content=_i18n.t("didnt_catch_time", state.language)
        + _render_range_offer(state.offered_slots, state.language),
        kind="slot_offer",
        new_picker_state=state,  # keep state unchanged
    )


async def _details_link_fallback(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    state: BookingPickerState,
) -> conversational.BookingReply:
    """After too many failed detail attempts, hand the booking link + clear state
    (so the customer is never trapped in the details loop)."""
    short_url = None
    if state.provider_id is not None and state.service_id is not None:
        short_url = await _mint_short_link(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            provider_id=state.provider_id,
            service_id=state.service_id,
        )
    lang = state.language
    tail = (
        _i18n.t("tail_finish_here", lang, url=short_url)
        if short_url is not None
        else _i18n.t("tail_help", lang)
    )
    return conversational.BookingReply(
        content=_i18n.t("no_back_and_forth", lang, tail=tail),
        kind="book_link",
        new_picker_state=None,  # clear the picker
    )


async def _picker_awaiting_details(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    text: str,
    state: BookingPickerState,
    chatwoot: object | None = None,
    cw_account_id: int | None = None,
    cw_conv_id: int | None = None,
) -> conversational.BookingReply:
    """Collect Name + Email + Phone, then move to the final summary.

    Hybrid parse: email/phone by regex (deterministic), name by the Haiku helper
    (only while still missing). Email + phone are **required** — we keep re-asking
    for whatever is missing, capped by ``_MAX_DETAIL_ATTEMPTS`` so a customer who
    can't supply valid details is handed the link instead of looping forever.

    The customer can still change their mind here: "another date" (or a bare new
    date) re-routes back to date selection instead of being counted as a failed
    detail attempt — otherwise they'd be stonewalled until the cap.
    """
    normalized = text.strip().lower()
    # "another date" → re-route to the date prompt (don't treat as a detail).
    if normalized in _ANOTHER_DATE_PHRASES:
        return _ask_for_date_again(state)

    # A message that carries NO contact info but DOES look like a date (has a
    # digit) is the customer changing their date mid-details — re-handle it as a
    # fresh date pick. Gated deterministically (regex email/phone + a digit) so a
    # bare name that happens to be a month-word ("May", "June") is never mistaken
    # for a date, and so the only LLM parse is the authoritative tenant-tz one
    # inside _picker_awaiting_date (no wasted name/UTC-gate calls). A phone is
    # caught first, so a digit-only contact reply is never re-routed.
    if (
        _extract_email(text) is None
        and _extract_phone(text) is None
        and any(ch.isdigit() for ch in text)
    ):
        return await _picker_awaiting_date(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            text=text,
            state=replace(state, step="awaiting_date"),
            chatwoot=chatwoot,
            cw_account_id=cw_account_id,
            cw_conv_id=cw_conv_id,
        )

    new_state = await _apply_contact_fields(state, text)
    missing = _missing_detail_fields(new_state)
    if not missing:
        # All required fields captured → final summary (awaiting_confirm).
        return _render_summary_reply(
            replace(new_state, detail_attempts=0),
            lead=_i18n.t("lead_thanks", new_state.language),
        )

    # Still missing required fields → re-ask, capped so we never trap the customer.
    attempts = new_state.detail_attempts + 1
    if attempts >= _MAX_DETAIL_ATTEMPTS:
        return await _details_link_fallback(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            state=new_state,
        )
    return conversational.BookingReply(
        content=_i18n.t(
            "details_still_need",
            state.language,
            labels=_missing_labels(missing, state.language),
        ),
        kind="slot_confirm",
        new_picker_state=replace(new_state, detail_attempts=attempts),
    )


async def _picker_awaiting_confirm(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    text: str,
    state: BookingPickerState,
) -> conversational.BookingReply:
    """FINAL confirm — the ONLY place EA is called.

    A free-flow yes books; an edit (a new time, or any detail) re-resolves just
    that piece and re-renders the summary. A bail phrase exits; an explicit "no"
    is acknowledged with a graceful change/cancel offer; anything else re-asks.
    Edits are checked BEFORE consent so "yes, but change my email to X" updates
    rather than books the stale data.
    """
    from polaris_api.services.escalation import interpret_consent

    new_state, changed = await _apply_edit(state, text)
    if changed:
        return _render_summary_reply(new_state, lead=_i18n.t("lead_updated", new_state.language))

    consent = interpret_consent(text)
    if consent == "yes":
        return await _do_inchat_book(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            state=state,
        )

    # Explicit "no" → acknowledge and offer a graceful path, rather than nagging.
    if consent == "no":
        return conversational.BookingReply(
            content=_i18n.t("confirm_no", state.language),
            kind="slot_confirm",
            new_picker_state=state,  # keep state unchanged
        )

    # Unclear → re-ask.
    return conversational.BookingReply(
        content=_i18n.t("confirm_unclear", state.language),
        kind="slot_confirm",
        new_picker_state=state,  # keep state unchanged
    )


async def _do_inchat_book(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    state: BookingPickerState,
) -> conversational.BookingReply:
    """Create the EA appointment and confirm in-chat (Phase 2 booking step)."""
    from polaris_api.services.booking.client import EABookingError, get_ea_booking_client

    if not state.resolved_slot or state.service_id is None or state.provider_id is None:
        logger.error(
            "booking_command picker: book called with incomplete state tenant=%s",
            tenant_id,
        )
        return conversational.BookingReply(
            content=_i18n.t("went_wrong", state.language),
            kind="error",
            new_picker_state=None,
        )

    # Build the EA customer dict from the details collected in awaiting_details.
    # snake_case keys per EA's Polaris contract. Each field falls back to the
    # verified handle only if somehow absent (the details step requires email +
    # phone, so in practice these are the customer's real details — EA no longer
    # synthesizes a placeholder email).
    first_name, last_name = _split_name(state.customer_name)
    customer: dict[str, str] = {
        "first_name": first_name or customer_handle,
        # EA's create_appointment rejects a blank last_name, which a single-token
        # name (every all-Chinese name, mononyms like "Cher"/"Siti") would produce —
        # mirror the proven flow-node fallback so the booking still goes through. The
        # customer-facing chat uses state.customer_name, so they never see "Unknown".
        "last_name": last_name or "Unknown",
        "phone": state.customer_phone or customer_handle,
        "email": state.customer_email or "",
    }

    client = get_ea_booking_client()

    # Parse the chosen slot ONCE; reuse start_dt/end_dt for both the EA call
    # and the Polaris row below. Derive the end from the service's real
    # duration — EA honours the end we pass, so a fixed guess would mis-size
    # the appointment and could block the adjacent slot.
    try:
        start_dt = datetime.fromisoformat(state.resolved_slot)
    except (ValueError, TypeError):
        logger.error(
            "booking_command picker: invalid resolved_slot %r tenant=%s",
            state.resolved_slot,
            tenant_id,
        )
        return conversational.BookingReply(
            content=_i18n.t("went_wrong", state.language),
            kind="error",
            new_picker_state=None,
        )
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    duration_minutes = await _resolve_service_duration_minutes(
        client, tenant_id=tenant_id, service_id=state.service_id
    )
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    try:
        appointment = await client.create_appointment(
            tenant_id=tenant_id,
            service_id=state.service_id,
            provider_id=state.provider_id,
            customer=customer,
            start_datetime=state.resolved_slot,
            end_datetime=end_dt.isoformat(),
        )
    except EABookingError:
        logger.warning(
            "booking_command picker: EA create_appointment failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        # Slot may have been taken — re-offer the day's ranges (back to pick).
        back_state = replace(state, step="awaiting_pick", resolved_slot=None)
        return conversational.BookingReply(
            content=_i18n.t("slot_unavailable", state.language)
            + _render_range_offer(state.offered_slots, state.language),
            kind="slot_offer",
            new_picker_state=back_state,
        )

    # Booking successful — insert the confirmed Polaris row via the pipeline's
    # RLS-scoped conn (same as the cancel/reschedule path in cancel_reschedule.py;
    # bookings is not a service-only table). Best-effort: EA is the source of truth.
    try:
        appointment_id = str(appointment["id"])
        await conn.execute(
            """
            INSERT INTO bookings
                (tenant_id, conversation_id, channel, customer_handle,
                 appointment_id, start_at, end_at, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'confirmed')
            ON CONFLICT DO NOTHING
            """,
            tenant_id,
            conversation_id,
            channel,
            customer_handle,
            appointment_id,
            start_dt,
            end_dt,
        )
    except Exception:  # noqa: BLE001 — best-effort DB write; EA is the source of truth
        logger.warning(
            "booking_command picker: Polaris bookings row insert failed tenant=%s",
            tenant_id,
            exc_info=True,
        )

    pretty = _format_iso_slot(state.resolved_slot, state.language)
    if state.customer_name:
        content = _i18n.t("booked_named", state.language, name=state.customer_name, pretty=pretty)
    else:
        content = _i18n.t("booked_unnamed", state.language, pretty=pretty)
    return conversational.BookingReply(
        content=content,
        kind="booked",
        new_picker_state=None,  # clear state — booking complete
    )
