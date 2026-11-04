"""Unit tests for demo lead flagging — ADR-066."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services import demo_lead, demo_sessions

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def test_flag_stamps_and_posts_note(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid4()
    mark = AsyncMock()
    monkeypatch.setattr(demo_sessions, "mark_lead_flagged", mark)
    cw = AsyncMock()

    await demo_lead.flag(
        session_id=sid,
        brand_name="TRG Watch",
        cw_client=cw,
        cw_account_id=5,
        cw_conversation_id=42,
    )

    mark.assert_awaited_once_with(sid)
    cw.post_private_note.assert_awaited_once()
    kwargs = cw.post_private_note.call_args.kwargs
    assert kwargs["account_id"] == 5
    assert kwargs["conversation_id"] == 42
    assert "TRG Watch" in kwargs["content"]


async def test_flag_stamps_only_when_no_cw_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid4()
    mark = AsyncMock()
    monkeypatch.setattr(demo_sessions, "mark_lead_flagged", mark)
    cw = AsyncMock()

    await demo_lead.flag(
        session_id=sid,
        brand_name="TRG Watch",
        cw_client=cw,
        cw_account_id=None,  # no CW link this turn
        cw_conversation_id=None,
    )

    mark.assert_awaited_once_with(sid)
    cw.post_private_note.assert_not_called()


async def test_flag_swallows_cw_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid4()
    mark = AsyncMock()
    monkeypatch.setattr(demo_sessions, "mark_lead_flagged", mark)
    cw = AsyncMock()
    cw.post_private_note.side_effect = RuntimeError("chatwoot down")

    # Must not raise — best-effort.
    await demo_lead.flag(
        session_id=sid,
        brand_name="TRG Watch",
        cw_client=cw,
        cw_account_id=5,
        cw_conversation_id=42,
    )
    mark.assert_awaited_once_with(sid)
