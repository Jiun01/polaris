"""In-chat slot-picker state for the new-booking Phase 2 flow.

Mirrors :class:`~polaris_api.services.escalation.EscalationState` —
same ``@dataclass(slots=True)`` + ``to_dict`` / ``from_dict`` pattern,
same fail-soft defensive coercion in ``from_dict``.

The pipeline persists this state to Redis under the ``"book:"`` prefix
(TTL ~30 min) via ``_load_booking_picker_state`` /
``_save_booking_picker_state`` in ``pipeline.py``.

Step lifecycle:
  ``awaiting_date``     — date-first picker: we asked the customer which date
                          they want (and gave a link); waiting for a date
                          reply. Only set by NEW offers; ``service_id`` /
                          ``provider_id`` are already bound, ``offered_slots``
                          is empty until a date is chosen.
  ``awaiting_pick``     — we offered availability (time ranges); waiting for
                          the customer to name a time (or ask for another
                          date). The whole day's slots live in
                          ``offered_slots``; we match the customer's stated
                          time against all of them (exact or nearest).
  ``awaiting_details``  — slot is agreed; we're collecting the customer's
                          name / email / phone before booking (Part B). The
                          chosen slot is held in ``resolved_slot`` and the
                          captured fields accumulate in
                          ``customer_name`` / ``customer_email`` /
                          ``customer_phone`` until all required ones are set.
  ``awaiting_confirm``  — customer picked a slot and we echoed it; waiting
                          for explicit "yes" / "no" confirmation before we
                          call ``create_appointment``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class BookingPickerState:
    """Per-conversation in-chat booking picker state (persisted as JSON)."""

    # "awaiting_date" | "awaiting_pick" | "awaiting_details" | "awaiting_confirm"
    step: str = "awaiting_pick"
    service_id: int | None = None
    provider_id: int | None = None
    # Full set of ISO8601 slot starts fetched from EA for this window.
    offered_slots: list[str] = field(default_factory=list)
    # The slot the customer selected (set once a time is matched).
    resolved_slot: str | None = None
    # Day-offset for "more" pagination (0 = today).
    offset_days: int = 0
    # Customer details collected in awaiting_details (Part B); None until captured.
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None
    # How many times we've re-asked for missing details (caps the loop so a
    # customer who can't give valid details is handed the link, never trapped).
    detail_attempts: int = 0
    # Reply language for the WHOLE flow — detected once on entry from the first
    # message and carried across every turn (a bare phone/email turn would
    # misdetect, so we never re-detect mid-flow). "en" | "ms" | "zh-cn".
    language: str = "en"

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "service_id": self.service_id,
            "provider_id": self.provider_id,
            "offered_slots": list(self.offered_slots),
            "resolved_slot": self.resolved_slot,
            "offset_days": self.offset_days,
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "customer_phone": self.customer_phone,
            "detail_attempts": self.detail_attempts,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> BookingPickerState:
        if not data:
            return cls()
        step = data.get("step", "awaiting_pick")
        service_raw = data.get("service_id")
        provider_raw = data.get("provider_id")
        slots_raw = data.get("offered_slots", [])
        offset_raw = data.get("offset_days", 0)
        name_raw = data.get("customer_name")
        email_raw = data.get("customer_email")
        phone_raw = data.get("customer_phone")
        attempts_raw = data.get("detail_attempts", 0)
        # Legacy/mid-deploy blobs have no "language" key → default "en". Only the
        # three supported codes survive coercion (anything else → "en").
        lang_raw = data.get("language", "en")
        return cls(
            step=str(step) if step is not None else "awaiting_pick",
            service_id=int(service_raw) if isinstance(service_raw, (int, float)) else None,
            provider_id=int(provider_raw) if isinstance(provider_raw, (int, float)) else None,
            offered_slots=list(slots_raw) if isinstance(slots_raw, list) else [],
            resolved_slot=str(data["resolved_slot"]) if data.get("resolved_slot") else None,
            offset_days=int(offset_raw) if isinstance(offset_raw, (int, float)) else 0,
            customer_name=str(name_raw) if name_raw else None,
            customer_email=str(email_raw) if email_raw else None,
            customer_phone=str(phone_raw) if phone_raw else None,
            detail_attempts=int(attempts_raw)
            if isinstance(attempts_raw, (int, float))
            else 0,
            language=lang_raw if lang_raw in ("en", "ms", "zh-cn") else "en",
        )


__all__ = ["BookingPickerState"]
