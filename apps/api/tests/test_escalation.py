"""Unit tests for the opt-in human-escalation state machine + consent reader.

Covers the pure decision logic (report don't-know handling). The pipeline
integration (Redis state, handoff side-effects) is verified on staging.
"""
from __future__ import annotations

import pytest
from polaris_api.services import escalation as e


def test_offer_only_on_second_consecutive_unhelpful() -> None:
    s = e.EscalationState()
    s, offer = e.register_reply(s, is_unhelpful=True)
    assert (s.unhelpful_streak, s.awaiting_consent, offer) == (1, False, False)
    s, offer = e.register_reply(s, is_unhelpful=True)
    assert (s.unhelpful_streak, s.awaiting_consent, offer) == (2, True, True)
    # Already awaiting → no repeat offer on the 3rd miss.
    s, offer = e.register_reply(s, is_unhelpful=True)
    assert (s.awaiting_consent, offer) == (True, False)


def test_helpful_reply_resets_streak() -> None:
    s = e.EscalationState(unhelpful_streak=1, awaiting_consent=False)
    s, offer = e.register_reply(s, is_unhelpful=False)
    assert (s.unhelpful_streak, s.awaiting_consent, offer) == (0, False, False)


def test_state_roundtrip_and_tolerates_garbage() -> None:
    assert e.EscalationState.from_dict(e.EscalationState(2, True).to_dict()) == (
        e.EscalationState(2, True)
    )
    assert e.EscalationState.from_dict(None) == e.EscalationState()
    assert e.EscalationState.from_dict({"junk": 1}) == e.EscalationState()


@pytest.mark.parametrize("msg,expected", [
    ("yes", "yes"), ("yes please", "yes"), ("ok connect me", "yes"),
    ("sure go ahead", "yes"), ("boleh", "yes"), ("ya", "yes"), ("好的", "yes"),
    ("可以", "yes"),
    ("no", "no"), ("no thanks", "no"), ("nah", "no"), ("tak nak", "no"),
    ("不用", "no"),
    ("what about the parking?", "unclear"), ("", "unclear"),
    ("how much is it", "unclear"),
])
def test_interpret_consent(msg: str, expected: str) -> None:
    assert e.interpret_consent(msg) == expected


def test_no_false_yes_inside_words() -> None:
    # "now" must not read as "no"; "yesterday" must not read as "yes".
    assert e.interpret_consent("can you do it now") == "unclear"
    assert e.interpret_consent("yesterday it worked") == "unclear"


@pytest.mark.parametrize("lang", ["en", "ms", "zh-cn", None, "xx"])
def test_offer_and_accept_text_always_resolve(lang) -> None:
    assert e.offer_text(lang)
    assert e.accept_text(lang)
