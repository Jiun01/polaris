"""Stage-3 handling-label sync (Chatwoot-native rehaul).

Pure-fn coverage of ``compute_handling_labels`` + behavioural coverage of
``sync_handling_label`` against a fake Chatwoot client (no IO). The sync keeps
exactly one of {bot-handling, human-handling} on a conversation while
preserving unrelated labels (Chatwoot's labels endpoint is set-style/replace).

asyncio_mode=auto → plain ``async def`` tests.
"""

from __future__ import annotations

from typing import cast

import pytest
from polaris_api.services.chatwoot.client import ChatwootClient, ChatwootError
from polaris_api.services.chatwoot.label_sync import (
    compute_handling_labels,
    sync_handling_label,
)

_BOT = "bot-handling"
_HUMAN = "human-handling"


class TestComputeHandlingLabels:
    def test_empty_bot(self) -> None:
        assert compute_handling_labels([], "bot") == [_BOT]

    def test_empty_human(self) -> None:
        assert compute_handling_labels([], "human") == [_HUMAN]

    def test_swaps_human_to_bot(self) -> None:
        assert compute_handling_labels([_HUMAN], "bot") == [_BOT]

    def test_swaps_bot_to_human(self) -> None:
        assert compute_handling_labels([_BOT], "human") == [_HUMAN]

    def test_preserves_other_labels(self) -> None:
        assert compute_handling_labels(["pol_bot_suspended"], "bot") == [
            "pol_bot_suspended",
            _BOT,
        ]

    def test_preserves_others_while_swapping(self) -> None:
        assert compute_handling_labels(["pol_bot_suspended", _HUMAN], "bot") == [
            "pol_bot_suspended",
            _BOT,
        ]

    def test_idempotent_no_duplicate(self) -> None:
        assert compute_handling_labels([_BOT], "bot") == [_BOT]

    def test_dedupes_non_handling(self) -> None:
        assert compute_handling_labels(["x", "x", _HUMAN], "bot") == ["x", _BOT]

    def test_preserves_order(self) -> None:
        assert compute_handling_labels(["a", "b", _BOT, "c"], "human") == [
            "a",
            "b",
            "c",
            _HUMAN,
        ]

    def test_collapses_both_handling_labels(self) -> None:
        # Pathological input carrying BOTH handling labels collapses to one.
        assert compute_handling_labels([_BOT, _HUMAN], "human") == [_HUMAN]


class _FakeCW:
    """Minimal stand-in recording the read-modify-write calls."""

    def __init__(self, current: list[str]) -> None:
        self._current = list(current)
        self.set_calls: list[list[str]] = []
        self.ensured: list[str] = []
        self.get_calls = 0

    async def get_conversation_labels(
        self, *, account_id: int, conversation_id: int
    ) -> list[str]:
        self.get_calls += 1
        return list(self._current)

    async def set_conversation_labels(
        self, *, account_id: int, conversation_id: int, labels: list[str]
    ) -> None:
        self.set_calls.append(list(labels))
        self._current = list(labels)

    async def ensure_label(self, *, account_id: int, label: str) -> bool:
        self.ensured.append(label)
        return True


def _as_client(fake: _FakeCW) -> ChatwootClient:
    return cast(ChatwootClient, fake)


class TestSyncHandlingLabel:
    async def test_skips_write_when_already_synced(self) -> None:
        cw = _FakeCW([_BOT])
        wrote = await sync_handling_label(
            account_id=1, conversation_id=2, owner="bot", chatwoot=_as_client(cw)
        )
        assert wrote is False
        assert cw.set_calls == []
        assert cw.ensured == []  # no provisioning when nothing changes
        assert cw.get_calls == 1

    async def test_swaps_and_writes(self) -> None:
        cw = _FakeCW([_HUMAN])
        wrote = await sync_handling_label(
            account_id=1, conversation_id=2, owner="bot", chatwoot=_as_client(cw)
        )
        assert wrote is True
        assert cw.set_calls == [[_BOT]]
        assert cw.ensured == [_BOT]

    async def test_preserves_other_labels_on_write(self) -> None:
        cw = _FakeCW(["pol_bot_suspended"])
        await sync_handling_label(
            account_id=1, conversation_id=2, owner="human", chatwoot=_as_client(cw)
        )
        assert cw.set_calls == [["pol_bot_suspended", _HUMAN]]

    async def test_already_synced_with_other_labels_skips(self) -> None:
        cw = _FakeCW(["pol_bot_suspended", _BOT])
        wrote = await sync_handling_label(
            account_id=1, conversation_id=2, owner="bot", chatwoot=_as_client(cw)
        )
        assert wrote is False
        assert cw.set_calls == []

    async def test_skips_when_set_matches_regardless_of_order(self) -> None:
        # Handling label present (different order) → set-equal → no spurious write.
        cw = _FakeCW([_BOT, "pol_bot_suspended"])
        wrote = await sync_handling_label(
            account_id=1, conversation_id=2, owner="bot", chatwoot=_as_client(cw)
        )
        assert wrote is False
        assert cw.set_calls == []

    async def test_rewrites_to_dedupe_duplicate_current(self) -> None:
        # Degenerate duplicate state is normalised, not treated as synced.
        cw = _FakeCW(["x", "x", _BOT])
        wrote = await sync_handling_label(
            account_id=1, conversation_id=2, owner="bot", chatwoot=_as_client(cw)
        )
        assert wrote is True
        assert cw.set_calls == [["x", _BOT]]

    async def test_propagates_chatwoot_error(self) -> None:
        class _RaisingCW(_FakeCW):
            async def get_conversation_labels(
                self, *, account_id: int, conversation_id: int
            ) -> list[str]:
                raise ChatwootError("boom")

        with pytest.raises(ChatwootError):
            await sync_handling_label(
                account_id=1,
                conversation_id=2,
                owner="bot",
                chatwoot=_as_client(_RaisingCW([])),
            )
