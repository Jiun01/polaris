"""Low-confidence → opt-in human escalation — QA report (don't-know handling).

The bot used to promise "I'll check with the team" with no mechanism behind it
(an empty promise — nothing reached a human). This implements the real flow the
operator chose:

  * Track consecutive UNHELPFUL bot replies (low retrieval confidence) per
    conversation. After ``_OFFER_AFTER_TRIES`` of them, the bot OFFERS to connect
    the customer to the team and arms "awaiting consent".
  * On the next turn, if the customer agrees (interpreted naturally), the
    conversation is escalated to a human (handoff) and the customer is invited to
    keep describing what they need, so the agent inherits the context. If they
    decline or ask something new, the streak resets and the bot answers normally.

This module is PURE + side-effect-free (state in / decision out) so the state
machine is unit-testable without a DB or LLM. The pipeline owns persistence
(Redis, keyed by conversation) and the actual handoff side-effects. Consent
interpretation here is a multilingual heuristic for the obvious cases; the
pipeline may consult the LLM when this returns ``"unclear"``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

# Offer to connect only after this many consecutive unhelpful replies (operator
# decision 2026-06-14: "2 tries, then ask if they want to connect").
_OFFER_AFTER_TRIES: Final[int] = 2

Consent = Literal["yes", "no", "unclear"]

# Language-keyed canned text (keys match ResponseEnvelope.language_detected:
# en / ms / zh-cn). Appended to the 2nd unhelpful reply (OFFER) and used as the
# escalation acknowledgement (ACCEPT). Kept short + plain (no em dashes per F9).
OFFER_TEXT: Final[dict[str, str]] = {
    "en": "I'm not able to fully answer that from what I have. Would you like me "
          "to connect you with our team? If so, just reply yes and tell me a bit "
          "more about what you need.",
    "ms": "Maaf, saya tak dapat jawab sepenuhnya dari maklumat yang ada. Nak saya "
          "sambungkan anda dengan pasukan kami? Kalau nak, balas ya dan beritahu "
          "sikit apa yang anda perlukan.",
    "zh-cn": "这部分我手上的资料无法完整回答。需要我帮您转接我们的团队吗？如果需要，"
             "请回复“好”并简单说明您的需求。",
}
ACCEPT_TEXT: Final[dict[str, str]] = {
    "en": "Sure, I'm connecting you with our team now. Please share a bit more "
          "about what you need so they can help you faster.",
    "ms": "Baik, saya sambungkan anda dengan pasukan kami sekarang. Sila kongsi "
          "lebih lanjut apa yang anda perlukan supaya mereka boleh bantu dengan cepat.",
    "zh-cn": "好的，我现在帮您转接团队。请多说明一下您的需求，方便他们更快协助您。",
}


def offer_text(language: str | None) -> str:
    return OFFER_TEXT.get(language or "en", OFFER_TEXT["en"])


def accept_text(language: str | None) -> str:
    return ACCEPT_TEXT.get(language or "en", ACCEPT_TEXT["en"])


@dataclass(slots=True)
class EscalationState:
    """Per-conversation escalation state (persisted as JSON by the pipeline)."""

    unhelpful_streak: int = 0
    awaiting_consent: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "unhelpful_streak": self.unhelpful_streak,
            "awaiting_consent": self.awaiting_consent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> EscalationState:
        if not data:
            return cls()
        streak = data.get("unhelpful_streak", 0)
        return cls(
            unhelpful_streak=streak if isinstance(streak, int) else 0,
            awaiting_consent=bool(data.get("awaiting_consent", False)),
        )


def register_reply(
    state: EscalationState, *, is_unhelpful: bool
) -> tuple[EscalationState, bool]:
    """Fold one bot reply's outcome into the state.

    ``is_unhelpful`` is the retrieval miss signal (no/low confidence) for the turn.
    Returns ``(new_state, should_offer)``. ``should_offer`` is True exactly on the
    transition where the streak reaches the offer threshold and we aren't already
    awaiting consent — the caller appends the offer text and the new state has
    ``awaiting_consent=True``. A helpful reply resets the streak.
    """
    if not is_unhelpful:
        return EscalationState(unhelpful_streak=0, awaiting_consent=False), False
    streak = state.unhelpful_streak + 1
    if streak >= _OFFER_AFTER_TRIES and not state.awaiting_consent:
        return EscalationState(unhelpful_streak=streak, awaiting_consent=True), True
    return EscalationState(
        unhelpful_streak=streak, awaiting_consent=state.awaiting_consent
    ), False


# Multilingual affirmative / negative cues for the obvious cases. Whole-word
# matching (so "no" doesn't fire inside "now"). The pipeline may defer to the LLM
# when this returns "unclear".
_YES: Final[frozenset[str]] = frozenset({
    "yes", "yes please", "yeah", "yep", "yup", "ok", "okay", "sure", "please",
    "please do", "connect", "connect me", "go ahead", "y",
    "ya", "yala", "boleh", "nak", "ok boleh", "baik",          # Malay
    "好", "好的", "要", "需要", "可以",                            # Chinese
})
_NO: Final[frozenset[str]] = frozenset({
    "no", "nope", "nah", "no thanks", "no thank you", "it's ok", "its ok",
    "tak", "tak nak", "tidak", "tak payah",                     # Malay
    "不", "不用", "不要", "没事",                                  # Chinese
})


def interpret_consent(message: str) -> Consent:
    """Heuristic read of a yes/no reply to the connect-to-team offer.

    Returns ``"yes"`` / ``"no"`` / ``"unclear"``. Designed for the obvious
    affirmatives/negatives across EN/BM/ZH; anything else is ``"unclear"`` so the
    caller can fall back to an LLM read or treat the message as a fresh question
    (never escalate without a clear yes).
    """
    t = message.strip().lower()
    if not t:
        return "unclear"
    # Exact short answer first (handles "ok", "boleh", CJK single words).
    if t in _YES:
        return "yes"
    if t in _NO:
        return "no"
    # Whole-word scan for longer replies ("yes please connect me").
    words = set(re.findall(r"[a-z']+", t))
    cjk = set(re.findall(r"[一-鿿]+", t))
    tokens = words | cjk
    hit_yes = bool(tokens & {w for w in _YES if " " not in w})
    hit_no = bool(tokens & {w for w in _NO if " " not in w})
    # Phrase contains check for multi-word cues.
    hit_yes = hit_yes or any(p in t for p in _YES if " " in p)
    hit_no = hit_no or any(p in t for p in _NO if " " in p)
    if hit_yes and not hit_no:
        return "yes"
    if hit_no and not hit_yes:
        return "no"
    return "unclear"
