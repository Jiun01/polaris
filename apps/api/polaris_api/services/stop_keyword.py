"""STOP keyword matcher — Phase 10 PR-1.

Three-language set (Q-PHASE10-01: native-speaker sign-off obtained 2026-05-22):
  EN:   STOP BOT REPLY, STOP, UNSUBSCRIBE  (STOP/UNSUBSCRIBE added 2026-06-14 —
        universal SMS opt-out convention; whole-message exact-match only)
  BM:   BERHENTI   (standard Bahasa Malaysia opt-out)
  ZH-S: 退订        (standard Simplified Chinese unsubscribe)

Matching rules:
  - The entire message body (after stripping surrounding whitespace) must
    equal the keyword.  We do NOT match the keyword as a substring — a
    user writing "please STOP BOT REPLY now" is NOT triggering STOP; they
    are asking the bot to stop its current reply flow, which is different.
    This is the same convention used by SMS STOP handlers (exact-match on
    stripped text, case-insensitive for Latin-script variants).
  - EN and BM are matched case-insensitively (``re.IGNORECASE``).
  - ZH-S is matched case-sensitively (Chinese script; no case concept).

``check(text)`` is pure-Python and synchronous — safe to call from the
pipeline hot path without async overhead.  The caller is responsible for
passing ``incoming.body`` (already stripped by the Meta webhook parser).

The function returns a ``StopMatch`` namedtuple:
  - ``matched``: bool
  - ``keyword``: the keyword string that matched, or None
"""

from __future__ import annotations

import re
from typing import NamedTuple

# ── keyword definitions ───────────────────────────────────────────────────────

# Q-PHASE10-01: native-speaker sign-off obtained 2026-05-22.

_KEYWORDS_CASE_INSENSITIVE: tuple[str, ...] = (
    "STOP BOT REPLY",  # EN (verbose, legacy)
    "STOP",            # EN (universal SMS opt-out convention; whole-message exact-
                       # match only, so "please stop" / "stop the order" do NOT match)
    "UNSUBSCRIBE",     # EN (universal opt-out; exact-match only)
    "BERHENTI",        # BM (Bahasa Malaysia — standard opt-out)
)

_KEYWORDS_EXACT: tuple[str, ...] = (
    "退订",  # ZH-S (Simplified Chinese — standard unsubscribe)
)

# Pre-compile: fullmatch on stripped text.
_CI_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"\A" + re.escape(kw) + r"\Z", re.IGNORECASE)
    for kw in _KEYWORDS_CASE_INSENSITIVE
)
_EXACT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"\A" + re.escape(kw) + r"\Z")
    for kw in _KEYWORDS_EXACT
)

# All keywords in declaration order (used for iteration).
_ALL_KEYWORDS: tuple[str, ...] = _KEYWORDS_CASE_INSENSITIVE + _KEYWORDS_EXACT
_ALL_PATTERNS: tuple[re.Pattern[str], ...] = _CI_PATTERNS + _EXACT_PATTERNS


# ── public API ────────────────────────────────────────────────────────────────


class StopMatch(NamedTuple):
    """Result of a STOP keyword check.

    ``matched``:  True iff the text is a STOP keyword.
    ``keyword``:  The canonical keyword string (as defined above) that
                  matched.  ``None`` when ``matched=False``.
    """

    matched: bool
    keyword: str | None


_NO_MATCH = StopMatch(matched=False, keyword=None)


def check(text: str) -> StopMatch:
    """Return a StopMatch for ``text``.

    ``text`` should be the raw inbound body; this function strips
    surrounding whitespace before matching.

    Examples::

        >>> check("stop bot reply")
        StopMatch(matched=True, keyword='STOP BOT REPLY')
        >>> check("berhenti")
        StopMatch(matched=True, keyword='BERHENTI')
        >>> check("退订")
        StopMatch(matched=True, keyword='退订')
        >>> check("please stop")
        StopMatch(matched=False, keyword=None)
    """
    stripped = text.strip()
    if not stripped:
        return _NO_MATCH
    for keyword, pattern in zip(_ALL_KEYWORDS, _ALL_PATTERNS, strict=True):
        if pattern.match(stripped):
            return StopMatch(matched=True, keyword=keyword)
    return _NO_MATCH
