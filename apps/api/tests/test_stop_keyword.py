"""STOP keyword matcher tests — Phase 10 PR-1."""

from __future__ import annotations

import pytest
from polaris_api.services.stop_keyword import StopMatch, check

# ── positive cases — each keyword variant ────────────────────────────────────


@pytest.mark.parametrize("text", [
    "STOP BOT REPLY",
    "stop bot reply",
    "Stop Bot Reply",
    "STOP BOT REPLY ",     # trailing space stripped
    "  stop bot reply  ",  # surrounding whitespace
])
def test_en_stop_keyword_matches(text: str) -> None:
    result = check(text)
    assert result.matched is True
    assert result.keyword == "STOP BOT REPLY"


@pytest.mark.parametrize("text", [
    "BERHENTI",
    "berhenti",
    "Berhenti",
    "  BERHENTI  ",
])
def test_bm_stop_keyword_matches(text: str) -> None:
    result = check(text)
    assert result.matched is True
    assert result.keyword == "BERHENTI"


@pytest.mark.parametrize("text", [
    "退订",
    "  退订  ",  # whitespace stripped
])
def test_zhs_stop_keyword_matches(text: str) -> None:
    result = check(text)
    assert result.matched is True
    assert result.keyword == "退订"


# ── negative cases — partial matches, substrings, similar text ───────────────


@pytest.mark.parametrize("text,keyword", [
    ("STOP", "STOP"),
    ("stop", "STOP"),
    ("  Stop  ", "STOP"),           # whitespace stripped, case-insensitive
    ("UNSUBSCRIBE", "UNSUBSCRIBE"),
    ("unsubscribe", "UNSUBSCRIBE"),
])
def test_en_universal_optout_matches(text: str, keyword: str) -> None:
    """STOP / UNSUBSCRIBE added 2026-06-14 (report F10) — whole-message only."""
    result = check(text)
    assert result.matched is True
    assert result.keyword == keyword


@pytest.mark.parametrize("text", [
    "please STOP BOT REPLY now",   # keyword as substring
    "stop the order",               # STOP as substring, not whole message
    "STOP BOT",                     # partial EN
    "BOT REPLY",                    # partial EN
    "henti",                        # old BM root — no longer a keyword
    "henti bot",                    # old BM phrase — no longer a keyword
    "停止机器人",                     # old ZH-S phrase — no longer a keyword
    "停止",                          # partial ZH-S
    "",                             # empty
    "   ",                          # whitespace only
    "hello world",
    "STOP BOT REPLY extra words",
])
def test_no_match(text: str) -> None:
    result = check(text)
    assert result.matched is False
    assert result.keyword is None


def test_returns_stop_match_namedtuple() -> None:
    result = check("stop bot reply")
    assert isinstance(result, StopMatch)
