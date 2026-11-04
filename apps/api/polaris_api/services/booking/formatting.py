"""Booking reply/slot formatting helpers — extracted from conversational.py
(Batch E, task E2; see PRP plan audit-batch-e-structural-splits).

Pure(ish) rendering helpers shared by the cancel/reschedule flow and the
in-chat picker state machine: slot/time formatting, short-link minting,
contact-detail extraction/rendering. No picker-state-machine control flow
lives here (that's ``picker_statemachine.py``).

``get_settings``, ``link_flow``, ``ensure_binding_configured`` are accessed
via ``conversational.<name>`` (not imported directly) in the functions that
need them, so the existing test suite's monkeypatches on
``polaris_api.services.booking.conversational.<name>`` keep working — see the
E2 GOTCHA in the PRP plan.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from polaris_api.services.booking import _i18n, conversational
from polaris_api.services.booking._i18n import AMPM, MONTHS, WEEKDAYS, coerce_lang
from polaris_api.services.booking.binding_token import BindingTokenError
from polaris_api.services.booking.cancel_reschedule import CustomerBooking
from polaris_api.services.booking.slot_picker_state import BookingPickerState

logger = logging.getLogger(__name__)


def _short_booking_link(jti: str) -> str:
    """The customer-facing short booking link: ``{public_api_base_url}/b/{jti}``.

    The full signed EA wizard URL is rebuilt server-side by the ``/b/{jti}``
    redirect at click time — we only ever send the short jti link.
    """
    return f"{conversational.get_settings().public_api_base_url.rstrip('/')}/b/{jti}"


async def _resolve_tenant_timezone(conn: asyncpg.Connection, tenant_id: UUID) -> str | None:
    """Read ``booking_config.tenant_timezone`` for customer-facing slot text.

    Fail-soft to ``None`` (which ``format_slot`` renders as UTC) so a config
    read error never breaks a cancel/reschedule reply.
    """
    try:
        from polaris_api.services.booking.config import read_booking_config

        booking_config = await read_booking_config(conn, tenant_id)
        return booking_config.get("tenant_timezone") or None
    except Exception:  # noqa: BLE001 — tz lookup is best-effort
        logger.warning(
            "booking_command: tenant_timezone read failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return None


def _format_slot(booking: CustomerBooking, tenant_timezone: str | None) -> str:
    """Human-readable slot line for a booking, localized to the tenant tz.

    Reuses ``_notify.format_slot`` (the same formatter the immediate booking
    confirmation uses) so cancel/reschedule replies show the customer's local
    time, not raw UTC. Falls back to UTC when ``tenant_timezone`` is unset or
    not a known IANA zone.
    """
    service = booking.service_name or "your appointment"
    if booking.start_at is None:
        return service
    from polaris_api.services.booking._notify import format_slot

    return f"{service} on {format_slot(booking.start_at, tenant_timezone)}"


def _format_iso_slot(iso: str, lang: object = "en") -> str:
    """Render an ISO8601 slot string as a human-friendly label.

    In-chat picker slots carry the PROVIDER's timezone offset (e.g.
    ``"2026-06-27T10:00:00+08:00"``), so the wall-clock time IS the customer's
    local time — we render it as-is and never convert to UTC (that conversion
    was the old skew bug that showed "06:00" for a 14:00 KL slot). Weekday/month
    are localized by indexing explicit tables (NOT glibc ``%a``/``%b``, which are
    locale-dependent and raise/misformat on Windows where the suite runs).
    Fail-soft to the raw string.
        en: ``Wed 1 Jul at 9:00 AM`` · ms: ``Rab, 1 Jul 9:00 pagi`` · zh: ``周三 7月1日 上午9:00``
    """
    lang_ = coerce_lang(lang)
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        wd = WEEKDAYS[lang_][dt.weekday()]
        mo = MONTHS[lang_][dt.month]
        tm = _fmt_12h(dt.hour, dt.minute, lang_)
        if lang_ == "zh-cn":
            return f"{wd} {mo}{dt.day}日 {tm}"
        if lang_ == "ms":
            return f"{wd}, {dt.day} {mo} {tm}"
        return f"{wd} {dt.day} {mo} at {tm}"
    except (ValueError, AttributeError):
        return iso


async def _mint_short_link(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    provider_id: int | None,
    service_id: int | None,
) -> str | None:
    """Mint a bound EA wizard short link tied to the verified handle.

    Returns the ``{public_api_base_url}/b/{jti}`` short link, or ``None`` when
    provider/service/binding is unavailable or the pending-booking insert fails
    (the caller decides how to fall back). Shared by ``_mint_booking_link`` (the
    full link reply) and ``_do_inchat_offer`` / ``_empty_date_reply`` (which
    compose the URL into a larger message).
    """
    # Without a provider+service or a usable binding secret we can't mint a
    # bound link.
    binding_ok = True
    try:
        conversational.ensure_binding_configured()
    except BindingTokenError:
        binding_ok = False

    if provider_id is None or service_id is None or not binding_ok:
        logger.info(
            "booking_command new: unbound fallback tenant=%s provider=%s service=%s binding_ok=%s",
            tenant_id,
            provider_id,
            service_id,
            binding_ok,
        )
        return None

    try:
        pending = await conversational.link_flow.create_pending_booking(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            customer_handle=customer_handle,
            service_name=None,  # Phase 1: skip extra EA call; EA fills name on confirm
            ea_service_id=service_id,
            ea_provider_id=provider_id,
        )
    except asyncpg.PostgresError:
        logger.exception("booking_command new: pending booking insert failed tenant=%s", tenant_id)
        return None

    return _short_booking_link(pending.jti)


async def _mint_booking_link(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    channel: str,
    customer_handle: str,
    provider_id: int | None,
    service_id: int | None,
    base_url: str,
) -> conversational.BookingReply:
    """Mint a bound EA wizard link, or fall back to the bare public page.

    Shared by ``_do_new_booking`` (Phase 1 link mode) and the in-chat picker's
    no-availability fallback. Returns ``kind="book_link"`` with a signed
    ``polaris_ref`` link, or ``kind="book_page"`` (the bare public URL) when
    provider/service/binding is unavailable or minting fails — never strands the
    customer.
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
    if short_url is None:
        return conversational.BookingReply(
            content=(
                f"*Book here:*\n{base_url}\n\nYou can also reply *HELP* if you need assistance."
            ),
            kind="book_page",
        )
    return conversational.BookingReply(
        content=(f"*Book here:*\n{short_url}\n_This link is valid for 1 hour._"),
        kind="book_link",
    )


_DEFAULT_SLOT_MINUTES = 30


def _fmt_12h(hour: int, minute: int, lang: object = "en") -> str:
    """Format a 24h wall-clock ``(hour, minute)`` as a 12h label (``"2:00 PM"``).

    ``hour`` is taken mod 24 so a range end that rolls to midnight renders sanely;
    Windows-portable (no ``%-I`` glibc directive). The AM/PM marker is localized:
    en ``9:00 AM`` / ms ``9:00 pagi`` (period after) / zh ``上午9:00`` (period before).
    """
    lang_ = coerce_lang(lang)
    hour %= 24
    hour12 = hour % 12 or 12
    am, pm = AMPM[lang_]
    period = am if hour < 12 else pm
    core = f"{hour12}:{minute:02d}"
    if lang_ == "zh-cn":
        return f"{period}{core}"
    return f"{core} {period}"


def _pretty_time(value: str, lang: object = "en") -> str:
    """Render an ``HH:MM`` time **or** an ISO slot as a 12h label (``"2:00 PM"``).

    The wall-clock fields are read directly (in-chat slots carry the provider-tz
    offset, so the wall clock IS the customer's local time — no conversion).
    Fail-soft to the raw input.
    """
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value)
            return _fmt_12h(dt.hour, dt.minute, lang)
        t = time.fromisoformat(value)
        return _fmt_12h(t.hour, t.minute, lang)
    except (ValueError, TypeError):
        return value


def _slot_minutes(iso: str) -> int | None:
    """Minutes-since-midnight (provider-local wall clock) for an ISO slot, or None."""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt.hour * 60 + dt.minute


def _compress_ranges(offered_slots: list[str]) -> list[tuple[int, int]]:
    """Compress slot start-times into ``(start_min, end_min)`` open-time ranges.

    Ranges are in minutes-since-midnight (provider-local wall clock). The step is
    inferred as the smallest positive gap between consecutive starts; a run breaks
    whenever the gap exceeds the step, so a booked mid-day hole splits the day into
    separate ranges (the "open ranges from ACTUAL slots" requirement). Each run's
    end is ``last_start + step``. A single slot yields
    ``(start, start + _DEFAULT_SLOT_MINUTES)``. Returns ``[]`` for no slots.
    """
    mins = sorted({m for m in (_slot_minutes(s) for s in offered_slots) if m is not None})
    if not mins:
        return []
    if len(mins) == 1:
        return [(mins[0], mins[0] + _DEFAULT_SLOT_MINUTES)]
    gaps = [b - a for a, b in zip(mins, mins[1:], strict=False) if b - a > 0]
    step = min(gaps) if gaps else _DEFAULT_SLOT_MINUTES
    ranges: list[tuple[int, int]] = []
    run_start = prev = mins[0]
    for m in mins[1:]:
        if m - prev == step:
            prev = m
            continue
        ranges.append((run_start, prev + step))
        run_start = prev = m
    ranges.append((run_start, prev + step))
    return ranges


def _render_range_offer(offered_slots: list[str], lang: object = "en") -> str:
    """Render the day's availability as open time ranges + "what time works?".

    e.g. ``"On Tue 30 Jun we're open 10:00 AM–4:00 PM and 4:30–6:00 PM. What time
    works for you?"`` Uses the slots' own date for the label (wall-clock local).
    Sentence + conjunction + date/time formatting all localized to ``lang``.
    """
    lang_ = coerce_lang(lang)
    ranges = _compress_ranges(offered_slots)
    if not ranges:
        return _i18n.t("range_none", lang_)
    label = ""
    for s in offered_slots:
        try:
            label = _format_date_label(datetime.fromisoformat(s).date(), lang_)
            break
        except (ValueError, TypeError):
            continue
    parts = [
        f"{_fmt_12h(a // 60, a % 60, lang_)}–{_fmt_12h(b // 60, b % 60, lang_)}" for a, b in ranges
    ]
    conj = _i18n.AND[lang_]
    if len(parts) == 1:
        windows = parts[0]
    elif len(parts) == 2:
        windows = f"{parts[0]} {conj} {parts[1]}"
    else:
        windows = ", ".join(parts[:-1]) + f", {conj} {parts[-1]}"
    if label:
        return _i18n.t("range_offer_with_label", lang_, label=label, windows=windows)
    return _i18n.t("range_offer_no_label", lang_, windows=windows)


def _match_time(offered_slots: list[str], target_hhmm: str) -> tuple[str | None, bool]:
    """Match a requested ``HH:MM`` against the day's slots → ``(slot_iso, exact)``.

    Returns the offered slot nearest the target (by absolute minutes), with
    ``exact=True`` when an offered slot starts exactly at the target. ``slot_iso``
    is ``None`` only when there are no parseable offered slots or the target time
    is malformed. Deterministic — no LLM, no I/O. This matches the **full** day
    (the old ``offered_slots[:5]`` cap that dead-ended noon/afternoon is gone).
    """
    try:
        t = time.fromisoformat(target_hhmm)
    except (ValueError, TypeError):
        return None, False
    target_min = t.hour * 60 + t.minute
    best: str | None = None
    best_diff: int | None = None
    exact = False
    for s in offered_slots:
        m = _slot_minutes(s)
        if m is None:
            continue
        diff = abs(m - target_min)
        if best_diff is None or diff < best_diff:
            best, best_diff, exact = s, diff, diff == 0
    return best, exact


def _format_date_label(d: date, lang: object = "en") -> str:
    """Short human date label, e.g. ``"Fri, 27 Jun"`` (Windows-portable).

    Localized via explicit weekday/month tables: en/ms ``Fri, 27 Jun`` /
    ``Jum, 27 Jun`` · zh ``周五 6月27日``.
    """
    lang_ = coerce_lang(lang)
    wd = WEEKDAYS[lang_][d.weekday()]
    mo = MONTHS[lang_][d.month]
    if lang_ == "zh-cn":
        return f"{wd} {mo}{d.day}日"
    return f"{wd}, {d.day} {mo}"


def _hours_to_iso(chosen_date: date, hours: list[str], tz: str) -> list[str]:
    """Convert EA provider-tz time-only hours to provider-tz ISO slot starts.

    EA's ``/api/v1/availabilities`` returns ``"HH:MM"`` strings in the PROVIDER's
    timezone for a single date. We stamp the provider tz explicitly so the booked
    instant is correct — rendering and ``_do_inchat_book``'s ``fromisoformat``
    then carry the right offset (no UTC skew; same lesson as the confirm-callback
    tz fix). Defensive: also accepts a full ISO datetime if a future EA build
    returns one, and skips anything unparseable.
    """
    try:
        zone: ZoneInfo = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")

    out: list[str] = []
    for hhmm in hours:
        if not isinstance(hhmm, str) or not hhmm.strip():
            continue
        raw = hhmm.strip()
        try:
            t = time.fromisoformat(raw)
        except ValueError:
            # Not a bare time — try a full ISO datetime, else skip. EA's contract
            # is "hours are in the provider's timezone", so we trust the
            # wall-clock and re-stamp the provider zone rather than any embedded
            # offset (which, if EA ever emitted a wrong one, would skew the slot).
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                continue
            out.append(datetime.combine(dt.date(), dt.time(), tzinfo=zone).isoformat())
            continue
        out.append(datetime.combine(chosen_date, t, tzinfo=zone).isoformat())
    return out


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


_PHONE_RE = re.compile(r"(?:\+?60|0)\d[\d\s\-]{6,12}\d")


def _normalize_phone(raw: str) -> str | None:
    """Normalise a matched phone to ``+<digits>`` / ``<digits>``, or None.

    Strips spaces/dashes/parens, keeps a leading ``+``, and validates the digit
    count (9–12, covering MY ``0123456789`` / ``+60123456789`` / ``012 345 6789``).
    """
    cleaned = re.sub(r"[\s\-()]", "", raw.strip())
    digits = re.sub(r"\D", "", cleaned)
    if not (9 <= len(digits) <= 12):
        return None
    return f"+{digits}" if cleaned.startswith("+") else digits


def _extract_email(text: str) -> str | None:
    """First email address in ``text`` (deterministic), or None."""
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else None


def _extract_phone(text: str) -> str | None:
    """First phone in ``text``, normalised — or None if absent/invalid."""
    m = _PHONE_RE.search(text)
    return _normalize_phone(m.group(0)) if m else None


def _split_name(full: str | None) -> tuple[str, str]:
    """Split a full name into ``(first, last)`` for EA's customer dict.

    One word → ``(word, "")``; multiple → first word + the rest; empty → ``("", "")``.
    """
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _missing_labels(missing: list[str], lang: str = "en") -> str:
    """Joined human labels for the missing fields, e.g. "name, email and phone number"."""
    return _i18n.join_and([_i18n.t("label_" + f, lang) for f in missing], lang)


def _ask_for_details(missing: list[str], lang: str = "en") -> str:
    """Combined friendly ask for the still-missing fields (single "your")."""
    return _i18n.t("details_ask", lang, labels=_missing_labels(missing, lang))


def _render_booking_summary(state: BookingPickerState) -> str:
    """The final confirmation summary (slot + collected details)."""
    pretty = _format_iso_slot(state.resolved_slot or "", state.language)
    return _i18n.t(
        "summary",
        state.language,
        pretty=pretty,
        name=state.customer_name,
        email=state.customer_email,
        phone=state.customer_phone,
    )


def _render_summary_reply(
    state: BookingPickerState, *, lead: str | None = None
) -> conversational.BookingReply:
    """Move to ``awaiting_confirm`` and render the final summary (with an optional
    lead line like "Updated." / "Thank you!")."""
    summary = _render_booking_summary(state)
    content = f"{lead}\n\n{summary}" if lead else summary
    return conversational.BookingReply(
        content=content,
        kind="slot_confirm",
        new_picker_state=replace(state, step="awaiting_confirm"),
    )
