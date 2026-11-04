"""Booking-command intent detection — extracted from conversational.py (Batch E,
task E2; see PRP plan audit-batch-e-structural-splits).

Pure phrase-table + regex detectors for cancel/reschedule/confirm/book intent,
plus the date-ish / booking-ish keyword gates that feed the LLM fallback in
``conversational.handle_booking_command``. No DB writes, no EA calls (the one
DB read here — ``_inchat_picker_on`` — is a best-effort feature-flag check).
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

import asyncpg

from polaris_api.services import llm as llm_service

logger = logging.getLogger(__name__)

BookingCommand = str  # "cancel" | "reschedule" | "confirm" | "book"


_RESCHEDULE_PHRASES: tuple[str, ...] = (
    "reschedule",
    "re-schedule",
    "change my booking",
    "change my appointment",
    "change the booking",
    "change the appointment",
    "move my booking",
    "move my appointment",
    "postpone my booking",
    "postpone my appointment",
    "different time",
    "another time",
    "new time",
    # Bahasa Melayu
    "tukar masa",
    "ubah masa",
    "tukar tarikh",
    "ubah tarikh",
    "jadual semula",
    # 中文
    "改时间",
    "改期",
    "改约",
    "重新预约",
    "改預約",
)


_CANCEL_PHRASES: tuple[str, ...] = (
    "cancel my booking",
    "cancel my appointment",
    "cancel the booking",
    "cancel the appointment",
    "cancel booking",
    "cancel appointment",
    "cancel my reservation",
    "cancel reservation",
    # Bahasa Melayu
    "batalkan tempahan",
    "batal tempahan",
    "batalkan temujanji",
    "batal temujanji",
    "nak batal",
    # 中文
    "取消预约",
    "取消预订",
    "取消預約",
    "取消預訂",
)


_NEW_BOOKING_PHRASES: tuple[str, ...] = (
    # English
    "book an appointment",
    "make a booking",
    "book appointment",
    "schedule an appointment",
    "set up an appointment",
    "reservation",
    "make an appointment",
    "book a slot",
    "get an appointment",
    # Desire + appointment-noun combos — high-precision so they never catch
    # "book a hotel/table/flight" (ambiguous verb phrasings) NOR "i have an
    # appointment" (no desire verb). The gated LLM fallback covers the rest.
    "want a booking",
    "like a booking",
    "need a booking",
    "want an appointment",
    "like an appointment",
    "need an appointment",
    # Bahasa Melayu
    "buat temujanji",
    "nak tempah",
    "tempah",
    "buat tempahan",
    # 中文
    "预约",
    "預約",
    "我要预约",
    "约时间",
)


_BARE_CANCEL = frozenset({"cancel", "batal", "取消"})


_BARE_RESCHEDULE = frozenset({"reschedule", "tukar masa", "改期"})


_BARE_CONFIRM = frozenset({"confirm", "ok", "okay", "yes", "ya", "sah", "确认", "確認"})


_BARE_BOOK = frozenset({"book", "booking", "appointment", "tempah", "预约", "預約"})


_BOOKING_KEYWORDS: tuple[str, ...] = (
    # English
    "book",
    "appointment",
    "appt",
    "reservation",
    "reserve",
    "slot",
    "schedule",
    "cancel",
    # Availability phrasings (a customer asking what's open is booking intent —
    # kept booking-specific so common words like "open"/"free"/"when" don't
    # over-trigger the classifier; dated phrasings are caught by _looks_date_ish).
    "available",
    "availability",
    "openings",
    "vacancy",
    "vacancies",
    # Bahasa Melayu
    "tempah",
    "temujanji",
    "tarikh",
    "batal",
    # 中文
    "预约",
    "預約",
    "预订",
    "預訂",
    "改期",
    "取消",
)


def _looks_booking_ish(text: str) -> bool:
    """True when the message contains a booking keyword (gates the LLM fallback)."""
    normalized = text.lower()
    return any(kw in normalized for kw in _BOOKING_KEYWORDS)


_DATE_ISH_RE = re.compile(
    r"\b(?:"
    # English + Bahasa Melayu month abbreviations sharing/extending the prefix
    # (mac=March, mei=May, ogos=Aug, okt=Oct, dis=Dec — the rest share an English
    # prefix so jan/feb/apr/jun/jul/sep/nov already match Malay too).
    r"\d{1,2}\s*(?:st|nd|rd|th)?\s*"
    r"(?:jan|feb|mar|mac|apr|may|mei|jun|jul|aug|ogos|sep|oct|okt|nov|dec|dis)[a-z]*"
    r"|(?:jan|feb|mar|mac|apr|may|mei|jun|jul|aug|ogos|sep|oct|okt|nov|dec|dis)[a-z]*\s+\d{1,2}"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    # Bahasa Melayu weekdays.
    r"|isnin|selasa|rabu|khamis|jumaat|sabtu|ahad"
    r"|tomorrow|today|tonight|tmr|tmrw"
    # Bahasa Melayu relative-day cues.
    r"|esok|lusa|hari\s+ini|minggu\s+depan|minggu\s+hadapan"
    r"|next\s+week|this\s+week"
    r"|\d{1,2}[/-]\d{1,2}"
    r")\b",
    re.IGNORECASE,
)


_DATE_ISH_CJK_RE = re.compile(
    r"[\d一二三四五六七八九十]\s*月"
    r"|今天|明天|后天|後天|大后天"
    r"|下周|下星期"
    r"|周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]"
)


def _looks_date_ish(text: str) -> bool:
    """True when the message names a concrete date cue — the cheap gate that lets a
    bare date ("1 july", "next monday", "7月1日", "5 ogos") reach the booking
    classifier so it never leaks to the agent (which fabricates availability)."""
    return bool(_DATE_ISH_RE.search(text)) or bool(_DATE_ISH_CJK_RE.search(text))


_MALAY_LANG_HINT_RE = re.compile(
    r"\b(?:ogos|mei|mac|okt|dis|isnin|selasa|rabu|khamis|jumaat|sabtu|ahad"
    r"|esok|lusa|pagi|petang|malam|nak|tempah|tempahan|boleh|pukul|tarikh)\b",
    re.IGNORECASE,
)


def _detect_booking_language(text: str) -> str:
    """Reply language for the picker: ``_detect_language`` + a Malay-vocab boost.

    CJK wins immediately ("zh-cn"); a confident stopword "ms" is kept. The boost
    only upgrades an "en" guess to "ms" when the message carries a distinctly-Malay
    booking token — so a terse Malay opener ("5 ogos") opens the picker in Malay
    instead of English. English openers contain none of these tokens."""
    lang = llm_service._detect_language(text)
    if lang == "en" and _MALAY_LANG_HINT_RE.search(text):
        return "ms"
    return lang


async def _inchat_picker_on(conn: asyncpg.Connection, tenant_id: UUID) -> bool:
    """True when the tenant has the in-chat picker enabled
    (``booking_config.inchat_picker``). Gates the bare-date interception so a date is
    never mis-routed for a tenant without the picker. Fail-soft to False."""
    try:
        from polaris_api.services.booking.config import read_booking_config

        booking_config = await read_booking_config(conn, tenant_id)
        return bool(booking_config.get("inchat_picker", False))
    except Exception:  # noqa: BLE001 — gate read is best-effort
        logger.warning(
            "booking_command: inchat_picker read failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return False


def detect_booking_command(text: str) -> BookingCommand | None:
    """Return ``"cancel"`` / ``"reschedule"`` / ``"confirm"`` / ``"book"`` or ``None``.

    Pure string matching — no DB, no LLM. Ordering matters:
      1. Reschedule phrases (most specific — "reschedule" contains "schedule")
      2. Cancel phrases
      3. New-booking phrases (checked after reschedule so "schedule an appointment"
         doesn't clobber "reschedule my appointment")
      4. Bare single-word replies (reschedule, cancel, confirm, book)
    """
    normalized = text.strip().lower()
    if not normalized:
        return None

    if any(p in normalized for p in _RESCHEDULE_PHRASES):
        return "reschedule"
    if any(p in normalized for p in _CANCEL_PHRASES):
        return "cancel"
    if any(p in normalized for p in _NEW_BOOKING_PHRASES):
        return "book"

    if normalized in _BARE_RESCHEDULE:
        return "reschedule"
    if normalized in _BARE_CANCEL:
        return "cancel"
    if normalized in _BARE_CONFIRM:
        return "confirm"
    # Bare "book" tokens matched only as the WHOLE message (mirror _BARE_CANCEL).
    if normalized in _BARE_BOOK:
        return "book"
    return None
