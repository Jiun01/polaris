"""Regression suite for the 2026-06-15 language-mirroring fixes.

Found via the live bot-reply E2E (relocated to ../docs/internal-ops/ops/e2e/bot_reply): a Mandarin question got a
Malay reply. Isolating it at the LLM layer surfaced TWO independent bugs:

  Finding 1 (generation): Malay conversation history intermittently overrode the
    detected-Chinese reply instruction. Fix = an emphatic per-turn
    <runtime_language> directive that overrides conversation momentum.
  Finding 2 (detection): _detect_language misclassified colloquial/Manglish Malay
    as English because the stopword list was formal-only and the threshold is >=2.
    Fix = expand _MALAY_STOPWORDS with colloquial terms.

These pin both fixes. Finding 1 is probabilistic at the model level (can't be unit
asserted), so we pin the PROMPT CONTRACT instead — the directive text must be present
and emphatic, which is what reduced the reversion rate.
"""

from __future__ import annotations

import pytest
from polaris_api.services import llm
from polaris_api.services._llm_prompts import build_response_system_prompt


# ---------------------------------------------------------------------------
# Finding 2 — _detect_language must classify colloquial Malay as "ms"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "korang ada buat sesi online tak?",   # the exact live-repro miss (was "en")
        "nak tanya, ada promo tak?",
        "korang ada kat sini ke?",
        "macam mana nak booking ah",
        "berapa harga untuk satu sesi",
    ],
)
def test_colloquial_malay_detected_as_ms(text: str) -> None:
    """BUG 2026-06-15-lang2: everyday spoken Malay was detected as English because
    only formal stopwords were listed. These must now resolve to 'ms'."""
    assert llm._detect_language(text) == "ms"


def test_cjk_still_detected_as_zh_cn() -> None:
    assert llm._detect_language("你们在哪里？地址是什么？") == "zh-cn"


def test_plain_english_still_detected_as_en() -> None:
    """Guard against over-eager Malay matching: clean English stays 'en'."""
    assert llm._detect_language("hi, what time do you open today?") == "en"
    assert llm._detect_language("do you accept credit card or only cash?") == "en"


# ---------------------------------------------------------------------------
# Finding 1 — the per-turn runtime-language directive must override history
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("language,name", [("zh-cn", "Simplified Chinese"), ("ms", "Bahasa Malaysia")])
def test_runtime_language_directive_overrides_conversation_history(language: str, name: str) -> None:
    """BUG 2026-06-15-lang1: a Malay history dragged a Chinese-detected message into
    a Malay reply. The fix is an emphatic <runtime_language> block instructing the
    model that the CURRENT message's language wins over earlier turns. Pin that the
    directive (a) names the language and (b) explicitly overrides prior turns, so a
    future prompt edit can't silently soften it back."""
    prompt = build_response_system_prompt("medium", language, None)  # type: ignore[arg-type]
    block = prompt[prompt.index("<runtime_language>"):prompt.index("</runtime_language>")]
    assert name in block
    # The override semantics — the words that fight conversation momentum.
    lowered = block.lower()
    assert "current message" in lowered
    assert "earlier" in lowered or "prior" in lowered or "previous" in lowered
    assert "switch" in lowered


# ---------------------------------------------------------------------------
# v1.0.42 — message-stream language nudge. The system-prompt directive alone lost
# to deep in-context history on Haiku (0/8 over 3 Chinese turns); a per-turn nudge
# appended LAST to the user content is the recency lever that fixed it (8/8).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("language,marker", [("zh-cn", "简体中文"), ("ms", "Bahasa Malaysia"), ("en", "English")])
def test_language_nudge_is_written_in_target_language(language: str, marker: str) -> None:
    """The nudge is in the TARGET language so it both instructs and primes the script."""
    assert marker in llm._language_nudge(language)  # type: ignore[arg-type]


def test_user_content_blocks_end_with_the_language_nudge() -> None:
    """BUG 2026-06-15-lang1 (residual): the nudge MUST be the LAST block of the user
    turn — that recency is what overrides deep cross-language history. If a refactor
    moves it earlier (or drops it), this fails."""
    blocks = llm._build_user_content_blocks("some rag context", "hello there", "zh-cn")  # type: ignore[arg-type]
    assert blocks[-1]["text"] == llm._language_nudge("zh-cn")  # type: ignore[arg-type]
    # The customer's actual message must still be present and precede the nudge.
    assert any(b["text"] == "hello there" for b in blocks[:-1])


def test_runtime_language_block_is_last_in_system_prompt() -> None:
    """Recency pin: the <runtime_language> directive is the final system block."""
    prompt = build_response_system_prompt("hard", "zh-cn", None)  # type: ignore[arg-type]
    assert prompt.rstrip().endswith("</runtime_language>")
