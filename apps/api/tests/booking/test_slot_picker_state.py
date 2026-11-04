"""Unit tests for BookingPickerState — to_dict / from_dict round-trip.

Pure-function, no DB, no LLM. Mirrors the EscalationState test style.
"""

from __future__ import annotations

from polaris_api.services.booking.slot_picker_state import BookingPickerState


def test_to_dict_round_trip() -> None:
    """All fields survive a to_dict → from_dict cycle."""
    state = BookingPickerState(
        step="awaiting_confirm",
        service_id=3,
        provider_id=7,
        offered_slots=["2026-06-24T09:00:00", "2026-06-24T09:30:00"],
        resolved_slot="2026-06-24T09:00:00",
        offset_days=14,
        customer_name="John Tan",
        customer_email="john@example.com",
        customer_phone="+60123456789",
    )
    d = state.to_dict()
    recovered = BookingPickerState.from_dict(d)

    assert recovered.step == "awaiting_confirm"
    assert recovered.service_id == 3
    assert recovered.provider_id == 7
    assert recovered.offered_slots == ["2026-06-24T09:00:00", "2026-06-24T09:30:00"]
    assert recovered.resolved_slot == "2026-06-24T09:00:00"
    assert recovered.offset_days == 14
    assert recovered.customer_name == "John Tan"
    assert recovered.customer_email == "john@example.com"
    assert recovered.customer_phone == "+60123456789"


def test_to_dict_awaiting_pick_no_resolved() -> None:
    """awaiting_pick step without a resolved_slot survives round-trip."""
    state = BookingPickerState(
        step="awaiting_pick",
        service_id=1,
        provider_id=2,
        offered_slots=["2026-07-01T10:00:00"],
        resolved_slot=None,
        offset_days=0,
    )
    d = state.to_dict()
    assert d["resolved_slot"] is None
    recovered = BookingPickerState.from_dict(d)
    assert recovered.resolved_slot is None
    assert recovered.step == "awaiting_pick"


def test_from_dict_none_returns_default() -> None:
    """from_dict(None) → default fresh state."""
    state = BookingPickerState.from_dict(None)
    assert state.step == "awaiting_pick"
    assert state.service_id is None
    assert state.provider_id is None
    assert state.offered_slots == []
    assert state.resolved_slot is None
    assert state.offset_days == 0
    assert state.customer_name is None
    assert state.customer_email is None
    assert state.customer_phone is None


def test_from_dict_empty_dict_returns_default() -> None:
    """from_dict({}) → default fresh state."""
    state = BookingPickerState.from_dict({})
    assert state.step == "awaiting_pick"


def test_from_dict_float_ids_coerced_to_int() -> None:
    """JSON numbers can come back as float after json.loads — must coerce."""
    state = BookingPickerState.from_dict({
        "step": "awaiting_pick",
        "service_id": 3.0,
        "provider_id": 7.0,
        "offered_slots": [],
        "resolved_slot": None,
        "offset_days": 0.0,
    })
    assert state.service_id == 3
    assert isinstance(state.service_id, int)
    assert state.provider_id == 7
    assert isinstance(state.provider_id, int)
    assert state.offset_days == 0
    assert isinstance(state.offset_days, int)


def test_from_dict_bad_service_id_becomes_none() -> None:
    """Non-numeric service_id → None (fail-soft)."""
    state = BookingPickerState.from_dict({
        "step": "awaiting_pick",
        "service_id": "not-a-number",
        "provider_id": None,
        "offered_slots": [],
        "resolved_slot": None,
        "offset_days": 0,
    })
    assert state.service_id is None
    assert state.provider_id is None


def test_from_dict_bad_offered_slots_becomes_empty() -> None:
    """Non-list offered_slots → [] (fail-soft)."""
    state = BookingPickerState.from_dict({
        "step": "awaiting_pick",
        "service_id": 1,
        "provider_id": 2,
        "offered_slots": "not-a-list",
        "resolved_slot": None,
        "offset_days": 0,
    })
    assert state.offered_slots == []


def test_from_dict_step_type_coercion() -> None:
    """Non-string step is cast to str defensively."""
    state = BookingPickerState.from_dict({
        "step": 42,
        "service_id": None,
        "provider_id": None,
        "offered_slots": [],
        "resolved_slot": None,
        "offset_days": 0,
    })
    assert state.step == "42"


def test_to_dict_contains_all_keys() -> None:
    """to_dict always includes every expected key."""
    state = BookingPickerState()
    d = state.to_dict()
    assert set(d.keys()) == {
        "step",
        "service_id",
        "provider_id",
        "offered_slots",
        "resolved_slot",
        "offset_days",
        "customer_name",
        "customer_email",
        "customer_phone",
        "detail_attempts",
        "language",
    }


def test_from_dict_tolerates_missing_customer_keys() -> None:
    """Old Redis blobs (pre-details fields) → customer fields default None."""
    state = BookingPickerState.from_dict({
        "step": "awaiting_pick",
        "service_id": 1,
        "provider_id": 2,
        "offered_slots": [],
        "resolved_slot": None,
        "offset_days": 0,
    })
    assert state.customer_name is None
    assert state.customer_email is None
    assert state.customer_phone is None


def test_from_dict_customer_fields_coerced_to_str() -> None:
    """Customer detail fields coerce to str (or None when falsy)."""
    state = BookingPickerState.from_dict({
        "customer_name": "Jane Doe",
        "customer_email": "jane@example.com",
        "customer_phone": "",  # falsy → None
    })
    assert state.customer_name == "Jane Doe"
    assert state.customer_email == "jane@example.com"
    assert state.customer_phone is None


def test_offered_slots_are_copied() -> None:
    """to_dict makes a copy of offered_slots so mutations don't bleed."""
    slots = ["2026-06-24T09:00:00"]
    state = BookingPickerState(offered_slots=slots)
    d = state.to_dict()
    slots.append("2026-06-25T09:00:00")
    assert d["offered_slots"] == ["2026-06-24T09:00:00"]  # not mutated
