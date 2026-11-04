"""Follow-up worker tests — Phase 13 follow-ups PR-1.

Exercises tick_due / _process_one with in-memory fakes + monkeypatched
external calls (secrets, snapshot, history, LLM, Meta). The decision logic
itself is covered by test_eligibility; here we prove the orchestration:
ineligible jobs are marked terminal WITHOUT sending, and an eligible job
generates + sends + marks sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from polaris_api.services.followups import worker as fw

pytestmark = pytest.mark.asyncio(loop_scope="function")

_NOW = datetime.now(UTC)


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[str] = []

    async def fetch(self, _sql: str, *_args: Any) -> list[dict[str, Any]]:
        return list(self._rows)

    async def execute(self, sql: str, *_args: Any) -> str:
        self.executed.append(sql)
        return "UPDATE 1"


class _FakeMeta:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []

    async def send_text(self, **kwargs: Any) -> str:
        self.sends.append(kwargs)
        return "wamid.followup.1"


class _Msg:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


class _Window:
    def __init__(self) -> None:
        self.messages = [_Msg("user", "do you have size M?"), _Msg("assistant", "Yes!")]


def _job_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": uuid4(),
        "tenant_id": uuid4(),
        "conversation_id": uuid4(),
        "anchor_inbound_at": _NOW - timedelta(hours=13),
        "attempts": 0,
        "channel": "whatsapp",
        "channel_user_id": "60123456789",
        "handoff_at": None,
        "tenant_settings": {"followup_config": {"enabled": True}},
        "phone_number_id": "PNID-TENANT",
        "latest_inbound_at": _NOW - timedelta(hours=13),  # == anchor → not superseded
        "opted_out_at": None,
    }
    base.update(over)
    return base


def _patch_externals(monkeypatch: pytest.MonkeyPatch, *, token: bytes | None = b"TOK") -> None:
    import polaris_api.services.secrets as secrets_mod
    from polaris_api.services import conversation as conv_mod
    from polaris_api.services import llm as llm_mod
    from polaris_api.services import tenant_snapshot as repo_mod

    async def _get(_c: Any, _t: Any, _k: str) -> bytes | None:
        return token

    async def _snap(_c: Any, _t: Any) -> dict[str, Any]:
        return {}

    async def _hist(**_k: Any) -> _Window:
        return _Window()

    async def _gen(**_k: Any) -> str:
        return "Just checking in — still after size M? Happy to help!"

    monkeypatch.setattr(secrets_mod, "get", _get)
    monkeypatch.setattr(repo_mod, "load_tenant_snapshot", _snap)
    monkeypatch.setattr(conv_mod, "load_history", _hist)
    monkeypatch.setattr(llm_mod, "generate_followup", _gen)


async def test_eligible_job_sends_and_marks_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_externals(monkeypatch)
    conn = _FakeConn([_job_row()])
    meta = _FakeMeta()
    sent = await fw.tick_due(conn, meta_client=meta)  # type: ignore[arg-type]
    assert sent == 1
    assert len(meta.sends) == 1
    assert meta.sends[0]["phone_number_id"] == "PNID-TENANT"
    assert meta.sends[0]["to"] == "60123456789"
    assert any("sent_at = now()" in s for s in conn.executed)


async def test_superseded_job_marked_terminal_without_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_externals(monkeypatch)
    # Customer replied after the job was scheduled → newer inbound than anchor.
    conn = _FakeConn([_job_row(latest_inbound_at=_NOW - timedelta(hours=1))])
    meta = _FakeMeta()
    sent = await fw.tick_due(conn, meta_client=meta)  # type: ignore[arg-type]
    assert sent == 0
    assert meta.sends == []  # never sent
    assert any("last_error" in s for s in conn.executed)  # marked terminal


async def test_handoff_job_marked_terminal_without_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_externals(monkeypatch)
    conn = _FakeConn([_job_row(handoff_at=_NOW - timedelta(hours=2))])
    meta = _FakeMeta()
    sent = await fw.tick_due(conn, meta_client=meta)  # type: ignore[arg-type]
    assert sent == 0
    assert meta.sends == []


async def test_missing_token_records_attempt_no_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_externals(monkeypatch, token=None)
    conn = _FakeConn([_job_row()])
    meta = _FakeMeta()
    sent = await fw.tick_due(conn, meta_client=meta)  # type: ignore[arg-type]
    assert sent == 0
    assert meta.sends == []
    assert any("attempts = attempts + 1" in s for s in conn.executed)
