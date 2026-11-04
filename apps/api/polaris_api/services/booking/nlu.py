"""Booking-specific LLM extraction helpers — extracted from services/llm.py
(Batch E, task E3; see PRP plan audit-batch-e-structural-splits).

``extract_booking_datetime`` / ``extract_customer_name`` / ``classify_booking_intent``
are the 3 Haiku-tier JSON-extract helpers the in-chat booking picker
(``booking/picker_statemachine.py``) and the cancel/reschedule/new-booking
hybrid detector (``booking/conversational.handle_booking_command``) use to
turn free text into structured booking data. They are booking-specific, not
general LLM plumbing, so they live here rather than in ``services/llm.py`` —
but they still need that module's generic Anthropic-call primitives
(``_get_anthropic_client``, ``_extract_text``, ``_extract_json_object``,
``_model_for_intent``), which are imported directly (no cycle: ``llm.py``
does not import anything from this module).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from datetime import time as dt_time

from polaris_api.config import get_settings
from polaris_api.services.llm import (
    _extract_json_object,
    _extract_text,
    _get_anthropic_client,
    _model_for_intent,
)

logger = logging.getLogger(__name__)

_BOOKING_INTENTS = frozenset({"cancel", "reschedule", "confirm", "book"})


async def extract_booking_datetime(
    message: str,
    *,
    today_iso: str,
    timezone: str,
) -> tuple[str | None, str | None]:
    """Extract a target calendar date (``YYYY-MM-DD``) and/or clock time
    (``HH:MM``, 24h) from a booking message.

    Resolves relative dates ("tomorrow", "next Monday", "the 30th") against
    ``today_iso`` in the tenant ``timezone`` and converts spoken times
    ("2pm"→"14:00", "noon"→"12:00", "9.30am"→"09:30"). A vague period
    ("afternoon", "morning") yields a ``None`` time. Returns
    ``(date | None, time | None)``.

    Each field is validated **independently** — ``date.fromisoformat`` +
    not-in-past for the date, ``time.fromisoformat`` for the time — so a bad
    time keeps a good date and vice versa (a membership-style guard so a
    malformed value can never reach EA). Either part may be ``None``; both are
    ``None`` on no match or any error.

    Same shape as the other booking Haiku helpers — Haiku tier, JSON-extract,
    fail-safe on any exception.
    """
    system = (
        "Extract the calendar date and clock time the customer wants for an "
        f"appointment. Today is {today_iso} ({timezone}). Resolve relative dates "
        '("tomorrow", "next Monday", "the 30th"). Convert times to 24-hour HH:MM '
        '("2pm"→"14:00", "noon"→"12:00", "9.30am"→"09:30"). Return ONLY '
        '{"date":"YYYY-MM-DD"|null,"time":"HH:MM"|null}. Use null for any part not '
        'clearly stated (a vague period like "afternoon" → time null). Never pick '
        "a date before today."
    )
    user = f"Customer message: {message}"
    try:
        settings = get_settings()
        model, _, _ = _model_for_intent(settings, "simple")
        client = _get_anthropic_client()
        # Short static system prompt below min cacheable block size — no cache_control.
        message_obj = await client.messages.create(
            model=model,
            max_tokens=100,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = _extract_text(message_obj.content).strip()
        parsed = json.loads(_extract_json_object(text))
        if not isinstance(parsed, dict):
            return None, None
        # Validate the date independently: reject malformed or past dates so we
        # never query EA for a day the customer can't book.
        out_date: str | None = None
        raw_date = parsed.get("date")
        if raw_date is not None:
            try:
                chosen = date.fromisoformat(str(raw_date))
                if chosen >= date.fromisoformat(today_iso):
                    out_date = chosen.isoformat()
            except ValueError:
                out_date = None
        # Validate the time independently: a malformed time is dropped, not fatal.
        out_time: str | None = None
        raw_time = parsed.get("time")
        if raw_time is not None:
            try:
                parsed_t = dt_time.fromisoformat(str(raw_time))
                out_time = f"{parsed_t.hour:02d}:{parsed_t.minute:02d}"
            except ValueError:
                out_time = None
        return out_date, out_time
    except Exception:  # noqa: BLE001 — fail-safe: never block the reply on extraction
        logger.warning("extract_booking_datetime failed — returning (None, None)")
        return None, None


async def extract_customer_name(message: str) -> str | None:
    """Extract the customer's full name from a chat message, or ``None``.

    Used by the in-chat booking details capture: emails and phone numbers are
    parsed deterministically by regex, so this helper is told to ignore them and
    return only a personal name. Returns ``None`` when no clear name is present
    (the caller then re-asks).

    Same shape as the other booking Haiku helpers — Haiku tier, JSON-extract,
    fail-safe to ``None`` on any exception.
    """
    system = (
        "Extract the person's full name from the message. Ignore email addresses "
        'and phone numbers. Return ONLY {"name":"<full name>"} or {"name":null} if '
        "no clear personal name is present."
    )
    user = f"Customer message: {message}"
    try:
        settings = get_settings()
        model, _, _ = _model_for_intent(settings, "simple")
        client = _get_anthropic_client()
        # Short static system prompt below min cacheable block size — no cache_control.
        message_obj = await client.messages.create(
            model=model,
            max_tokens=100,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = _extract_text(message_obj.content).strip()
        parsed = json.loads(_extract_json_object(text))
        raw = parsed.get("name") if isinstance(parsed, dict) else None
        if raw is None:
            return None
        name = str(raw).strip()
        return name or None
    except Exception:  # noqa: BLE001 — fail-safe: never block the reply on extraction
        logger.warning("extract_customer_name failed — returning None (no name resolved)")
        return None


async def classify_booking_intent(message: str) -> str | None:
    """Edge-case fallback: classify a booking command the phrase table missed.

    Returns EXACTLY one of ``"cancel" | "reschedule" | "confirm" | "book"`` (the
    customer's intent), or ``None`` when the message is not a booking command.
    Membership-checks the model output against the allowed set so a hallucinated
    label can never route. This is the LLM half of the hybrid detector — the
    deterministic phrase table in ``conversational.detect_booking_command`` handles
    the bulk; the caller only invokes this when a phrase missed AND the message
    looks booking-ish, so it fires on a small minority of traffic.

    Same shape as the other booking Haiku helpers — Haiku tier, JSON-extract,
    same fail-safe to ``None`` on any exception.
    """
    system = (
        "Classify a customer chat message about an appointment into ONE intent. "
        'Return ONLY a JSON object {"intent": "<value>"}. Choose <value> from:\n'
        '- "book": wants a NEW appointment. This includes asking what times or '
        "availability are open, asking if you are free on a day, or naming a day "
        "or time they want to come in. The day/time may be written in English, "
        "Bahasa Melayu, or Chinese — treat a bare date in any of these as "
        'wanting to book (e.g. "1 July", "are you free Monday", "5 Ogos", '
        '"esok", "isnin depan", "7月1日", "下星期一", "明天有空吗").\n'
        '- "cancel": wants to cancel an existing appointment.\n'
        '- "reschedule": wants to move an existing appointment to a different '
        "time.\n"
        '- "confirm": acknowledging or confirming an existing appointment.\n'
        "- null: none of the above. Use null when a date or time is only "
        "MENTIONED without wanting an appointment (e.g. an invoice or order date, "
        'a holiday, "since June", "I bought it on the 3rd").\n'
        "Use only those five values."
    )
    user = f"Customer message: {message}"
    try:
        settings = get_settings()
        model, _, _ = _model_for_intent(settings, "simple")
        client = _get_anthropic_client()
        # Short static system prompt below min cacheable block size — no cache_control.
        message_obj = await client.messages.create(
            model=model,
            max_tokens=100,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = _extract_text(message_obj.content).strip()
        parsed = json.loads(_extract_json_object(text))
        intent = parsed.get("intent") if isinstance(parsed, dict) else None
        if intent is None:
            return None
        intent = str(intent)
        # Membership-check: never route on a hallucinated label.
        return intent if intent in _BOOKING_INTENTS else None
    except Exception:  # noqa: BLE001 — fail-safe: never block the reply on classification
        logger.warning("classify_booking_intent failed — returning None (no intent resolved)")
        return None
