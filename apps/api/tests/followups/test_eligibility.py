"""Eligibility predicate tests — Phase 13 follow-ups PR-1."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polaris_api.services.followups.eligibility import (
    ScheduleSignals,
    SendSignals,
    should_schedule_followup,
    should_send_followup,
)

_T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _schedule(**over: object) -> ScheduleSignals:
    base: dict[str, object] = {
        "enabled": True,
        "channel": "whatsapp",
        "handoff_at": None,
        "last_inbound_at": _T0,
        "last_message_role": "assistant",
        "opted_out": False,
    }
    base.update(over)
    return ScheduleSignals(**base)  # type: ignore[arg-type]


def _send(**over: object) -> SendSignals:
    base: dict[str, object] = {
        "enabled": True,
        "channel": "whatsapp",
        "handoff_at": None,
        "latest_inbound_at": _T0,
        "anchor_inbound_at": _T0,
        "opted_out": False,
        "now": _T0 + timedelta(hours=13),
        "attempts": 0,
        "max_attempts": 3,
    }
    base.update(over)
    return SendSignals(**base)  # type: ignore[arg-type]


# ---- schedule gate ----


def test_schedule_happy_path() -> None:
    assert should_schedule_followup(_schedule()) is True


def test_schedule_blocked_when_disabled() -> None:
    assert should_schedule_followup(_schedule(enabled=False)) is False


def test_schedule_blocked_on_non_whatsapp() -> None:
    assert should_schedule_followup(_schedule(channel="messenger")) is False


def test_schedule_blocked_after_handoff() -> None:
    assert should_schedule_followup(_schedule(handoff_at=_T0)) is False


def test_schedule_blocked_when_customer_replied_last() -> None:
    # last message is the customer's → they're mid-conversation, not quiet.
    assert should_schedule_followup(_schedule(last_message_role="user")) is False


def test_schedule_blocked_when_opted_out() -> None:
    assert should_schedule_followup(_schedule(opted_out=True)) is False


def test_schedule_blocked_with_no_inbound() -> None:
    assert should_schedule_followup(_schedule(last_inbound_at=None)) is False


# ---- send gate ----


def test_send_happy_path() -> None:
    assert should_send_followup(_send()) is True


def test_send_blocked_when_superseded_by_newer_inbound() -> None:
    # Customer replied after the job was scheduled → cancel-on-inbound race.
    assert should_send_followup(
        _send(latest_inbound_at=_T0 + timedelta(hours=2))
    ) is False


def test_send_blocked_when_window_closed() -> None:
    assert should_send_followup(_send(now=_T0 + timedelta(hours=24))) is False


def test_send_allowed_just_inside_window() -> None:
    assert should_send_followup(
        _send(now=_T0 + timedelta(hours=23, minutes=59))
    ) is True


def test_send_blocked_after_handoff() -> None:
    assert should_send_followup(_send(handoff_at=_T0 + timedelta(hours=1))) is False


def test_send_blocked_when_attempts_exhausted() -> None:
    assert should_send_followup(_send(attempts=3, max_attempts=3)) is False


def test_send_blocked_when_opted_out() -> None:
    assert should_send_followup(_send(opted_out=True)) is False


def test_send_blocked_when_no_inbound_recorded() -> None:
    assert should_send_followup(_send(latest_inbound_at=None)) is False
