"""Handoff service tests (Phase 7 PR-3).

Two test surfaces:

  1. Pure helpers (no async, no IO): is_in_business_hours,
     _sanitize_for_private_note, substitute. These get the bulk of the
     coverage — they're the security-critical primitives (timezone
     boundary, Markdown injection neutralisation, variable substitution
     parity with send_template).

  2. Orchestration smoke (async, fake conn + fake clients): one happy
     path + one Chatwoot-fail recovery-marker write. Full E2E coverage
     of the orchestration lives in PR-7's e2e suite where a real DB
     + real Chatwoot stub round-trip the scenarios.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from polaris_api.services.chatwoot.client import (
    ChatwootStructuralError,
    ChatwootTransientError,
)
from polaris_api.services.chatwoot.handoff import (
    _sanitize_for_private_note,
    initiate_handoff,
    is_in_business_hours,
    substitute,
)

# Tenant timezone used throughout. Singapore = UTC+8, no DST — boundary
# tests are deterministic without daylight-savings interaction.
_TZ = "Asia/Singapore"


# =====================================================================
# is_in_business_hours
# =====================================================================


def _bh_mon_fri_9_to_18() -> dict[str, object]:
    """Standard 9-18 Mon-Fri schedule, Sat/Sun closed."""
    return {
        "0": {"open": "09:00", "close": "18:00"},
        "1": {"open": "09:00", "close": "18:00"},
        "2": {"open": "09:00", "close": "18:00"},
        "3": {"open": "09:00", "close": "18:00"},
        "4": {"open": "09:00", "close": "18:00"},
        "5": None,
        "6": None,
    }


@pytest.fixture
def freeze_now(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Freeze datetime.datetime.now used by is_in_business_hours."""
    holder: dict[str, datetime.datetime] = {}

    def _set(year: int, month: int, day: int, hour: int, minute: int) -> None:
        holder["now"] = datetime.datetime(
            year, month, day, hour, minute, tzinfo=ZoneInfo(_TZ)
        )

    class _FakeDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime.datetime:  # type: ignore[override]
            d = holder.get("now")
            if d is None:
                raise RuntimeError("freeze_now: call .set(...) first")
            return d.astimezone(tz) if tz is not None else d

    import polaris_api.services.chatwoot.handoff as handoff_mod

    monkeypatch.setattr(handoff_mod.datetime, "datetime", _FakeDateTime)

    class _Setter:
        set = staticmethod(_set)

    yield _Setter()


def test_business_hours_inside_window(freeze_now) -> None:
    # Tuesday (weekday=1) 14:00 Singapore — clearly in hours.
    freeze_now.set(2026, 5, 19, 14, 0)
    assert is_in_business_hours(
        business_hours=_bh_mon_fri_9_to_18(), timezone=_TZ
    ) is True


def test_business_hours_before_open(freeze_now) -> None:
    # Tuesday 08:59 — one minute before 09:00 open.
    freeze_now.set(2026, 5, 19, 8, 59)
    assert is_in_business_hours(
        business_hours=_bh_mon_fri_9_to_18(), timezone=_TZ
    ) is False


def test_business_hours_at_close_minute_is_after_hours(freeze_now) -> None:
    # Strict cutoff (Q-PHASE7-03): exactly 18:00 is after_hours.
    freeze_now.set(2026, 5, 19, 18, 0)
    assert is_in_business_hours(
        business_hours=_bh_mon_fri_9_to_18(), timezone=_TZ
    ) is False


def test_business_hours_weekend_closed(freeze_now) -> None:
    # Saturday (weekday=5) 14:00 — schedule has null for day "5".
    freeze_now.set(2026, 5, 23, 14, 0)
    assert is_in_business_hours(
        business_hours=_bh_mon_fri_9_to_18(), timezone=_TZ
    ) is False


def test_business_hours_grace_widens_window(freeze_now) -> None:
    # Tuesday 18:10 — 10 min after close. With 15-min grace, still in_hours.
    freeze_now.set(2026, 5, 19, 18, 10)
    assert (
        is_in_business_hours(
            business_hours=_bh_mon_fri_9_to_18(),
            timezone=_TZ,
            grace_minutes=15,
        )
        is True
    )
    # Same time, no grace → after_hours.
    assert (
        is_in_business_hours(
            business_hours=_bh_mon_fri_9_to_18(),
            timezone=_TZ,
            grace_minutes=0,
        )
        is False
    )


def test_business_hours_invalid_tz_falls_through_to_after_hours(freeze_now) -> None:
    freeze_now.set(2026, 5, 19, 14, 0)
    assert is_in_business_hours(
        business_hours=_bh_mon_fri_9_to_18(),
        timezone="Not/A/Timezone",
    ) is False


def test_business_hours_missing_business_hours_returns_false() -> None:
    assert is_in_business_hours(business_hours=None, timezone=_TZ) is False
    assert is_in_business_hours(business_hours={}, timezone=_TZ) is False


def test_business_hours_malformed_entry_returns_false(freeze_now) -> None:
    freeze_now.set(2026, 5, 19, 14, 0)
    # Garbled "open" string.
    bad = {"1": {"open": "9am", "close": "18:00"}}
    assert is_in_business_hours(business_hours=bad, timezone=_TZ) is False


# =====================================================================
# _sanitize_for_private_note
# =====================================================================


def test_sanitize_neutralises_markdown_link_brackets() -> None:
    """A malicious customer reply containing a Markdown link must be
    stripped of its bracket syntax before reaching Chatwoot."""
    payload = "Customer said: [click here](javascript:alert(1))"
    out = _sanitize_for_private_note(payload, max_len=4000)
    assert "[" not in out
    assert "]" not in out
    # Visible text survives.
    assert "click here" in out
    assert "javascript:alert(1)" in out  # remains visible as plain text


def test_sanitize_neutralises_backticks() -> None:
    out = _sanitize_for_private_note(
        "Customer said: `rm -rf /` because reasons", max_len=4000
    )
    assert "`" not in out
    assert "rm -rf /" in out  # plain text preserved


def test_sanitize_truncates_to_max_len() -> None:
    long_text = "x" * 5000
    out = _sanitize_for_private_note(long_text, max_len=100)
    assert len(out) == 100
    assert out.endswith("…")


def test_sanitize_no_op_on_clean_text() -> None:
    clean = "Customer asked about refund. Tier: medium."
    out = _sanitize_for_private_note(clean, max_len=4000)
    assert out == clean


# =====================================================================
# substitute
# =====================================================================


def test_substitute_replaces_known_vars() -> None:
    assert substitute("Hello {{name}}", {"name": "Alice"}) == "Hello Alice"


def test_substitute_missing_var_becomes_empty() -> None:
    assert substitute("Hello {{name}}", {}) == "Hello "


def test_substitute_none_value_becomes_empty() -> None:
    assert substitute("Hello {{name}}", {"name": None}) == "Hello "


def test_substitute_handles_no_variables() -> None:
    assert substitute("plain text", {"name": "Alice"}) == "plain text"


# =====================================================================
# initiate_handoff — orchestration smoke
# =====================================================================


def _ctx_row(
    *,
    chatwoot_account_id: int = 1,
    chatwoot_conversation_id: int = 42,
    channel: str = "whatsapp",
    channel_user_id: str = "60123456789",
    business_hours: dict | None = None,
    in_hours_msg: str = "An agent will reach out shortly.",
    after_hours_msg: str = "We're closed.",
    tenant_branch_id: UUID | None = None,
) -> dict[str, Any]:
    """Build the dict returned by _load_handoff_context."""
    return {
        "tenant_business_hours": business_hours or _bh_mon_fri_9_to_18(),
        "tenant_timezone": _TZ,
        "tenant_in_hours": in_hours_msg,
        "tenant_after_hours": after_hours_msg,
        "chatwoot_account_id": chatwoot_account_id,
        "chatwoot_conversation_id": chatwoot_conversation_id,
        "channel": channel,
        "channel_user_id": channel_user_id,
        "tenant_branch_id": tenant_branch_id,
        "branch_business_hours": None,
        "branch_in_hours": None,
        "branch_after_hours": None,
    }


@pytest.fixture
def fake_conn() -> Any:
    """asyncpg.Connection mock — fetchrow + execute + fetchval async stubs."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value="DEMO_PHONE_PID")
    return conn


@pytest.fixture
def fake_chatwoot() -> Any:
    cw = MagicMock()
    cw.flip_conversation_status = AsyncMock(return_value=None)
    cw.assign_team = AsyncMock(return_value=None)
    cw.apply_conversation_label = AsyncMock(return_value=None)
    # Stage-3 handling-label sync reads-then-writes via these.
    cw.get_conversation_labels = AsyncMock(return_value=[])
    cw.set_conversation_labels = AsyncMock(return_value=None)
    cw.ensure_label = AsyncMock(return_value=True)
    cw.post_private_note = AsyncMock(return_value=999)
    return cw


@pytest.fixture
def fake_meta() -> Any:
    m = MagicMock()
    m.send_text = AsyncMock(return_value="wamid.test.001")
    return m


@pytest.fixture
def fake_secrets_get(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub services.secrets.get to return a non-placeholder token."""
    import polaris_api.services.secrets as secrets_mod

    async def _get(conn: Any, tenant_id: UUID, kind: str) -> bytes:
        # services.secrets.get returns bytes (libsodium-decrypted blob);
        # handoff service decodes utf-8. Mirror the wire shape here.
        return b"EAA-tenant-token"

    monkeypatch.setattr(secrets_mod, "get", _get)


@pytest.mark.asyncio
async def test_handoff_confirmed_happy_path(
    freeze_now, fake_conn, fake_chatwoot, fake_meta, fake_secrets_get
) -> None:
    # Tuesday 14:00 — in hours.
    freeze_now.set(2026, 5, 19, 14, 0)
    fake_conn.fetchrow.return_value = _ctx_row()

    tenant_id = uuid4()
    conversation_id = uuid4()
    requested_at = datetime.datetime(2026, 5, 19, 13, 59, tzinfo=UTC)

    result = await initiate_handoff(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        team_id_str="3",
        private_note="Customer asked about refund. Tier: medium.",
        last_reply="I want a refund",
        intent_tier="medium",
        customer_name="Alice",
        requested_at=requested_at,
        conn=fake_conn,
        chatwoot=fake_chatwoot,
        meta_client=fake_meta,
    )

    assert result.outcome == "confirmed"
    assert result.business_hours_state == "in_hours"
    assert result.customer_message_sent is True
    assert result.chatwoot_status_flipped is True
    assert result.team_assigned is True
    assert result.private_note_posted is True
    assert result.customer_message_id == "wamid.test.001"
    # Customer message went out with the in-hours template (no vars in default).
    fake_meta.send_text.assert_awaited_once()
    send_kwargs = fake_meta.send_text.await_args.kwargs
    assert send_kwargs["body"] == "An agent will reach out shortly."
    # Chatwoot sequential calls all fired.
    fake_chatwoot.flip_conversation_status.assert_awaited_once()
    fake_chatwoot.assign_team.assert_awaited_once()
    fake_chatwoot.post_private_note.assert_awaited_once()
    # handoff_at + event row writes happened (4 execute calls expected:
    # 1 conversations.handoff_at, 1 tenants.handoff_at, 1 event row insert).
    assert fake_conn.execute.await_count >= 3


@pytest.mark.asyncio
async def test_handoff_after_hours_picks_alt_message(
    freeze_now, fake_conn, fake_chatwoot, fake_meta, fake_secrets_get
) -> None:
    # Tuesday 22:00 — after hours.
    freeze_now.set(2026, 5, 19, 22, 0)
    fake_conn.fetchrow.return_value = _ctx_row(
        in_hours_msg="IN-HOURS-MSG",
        after_hours_msg="AFTER-HOURS-MSG",
    )

    result = await initiate_handoff(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        team_id_str="3",
        private_note="note",
        last_reply=None,
        intent_tier=None,
        customer_name=None,
        requested_at=datetime.datetime.now(UTC),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
        meta_client=fake_meta,
    )

    assert result.business_hours_state == "after_hours"
    send_kwargs = fake_meta.send_text.await_args.kwargs
    assert send_kwargs["body"] == "AFTER-HOURS-MSG"


@pytest.mark.asyncio
async def test_handoff_chatwoot_transient_sets_recovery_marker(
    freeze_now, fake_conn, fake_chatwoot, fake_meta, fake_secrets_get
) -> None:
    freeze_now.set(2026, 5, 19, 14, 0)
    fake_conn.fetchrow.return_value = _ctx_row()
    # Chatwoot status flip raises transient → caller catches via gather.
    fake_chatwoot.flip_conversation_status.side_effect = ChatwootTransientError(
        "5xx after retries"
    )

    result = await initiate_handoff(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        team_id_str="3",
        private_note="note",
        last_reply="msg",
        intent_tier="medium",
        customer_name="Alice",
        requested_at=datetime.datetime.now(UTC),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
        meta_client=fake_meta,
    )

    assert result.outcome == "declined"
    assert result.customer_message_sent is True
    assert result.chatwoot_status_flipped is False
    # Recovery marker SQL was issued.
    update_calls = [
        c for c in fake_conn.execute.await_args_list
        if "handoff_pending_recovery_at" in str(c.args[0])
    ]
    assert len(update_calls) == 1, (
        f"Expected one recovery-marker UPDATE; got {fake_conn.execute.await_args_list}"
    )


@pytest.mark.asyncio
async def test_handoff_chatwoot_structural_returns_errored(
    freeze_now, fake_conn, fake_chatwoot, fake_meta, fake_secrets_get
) -> None:
    freeze_now.set(2026, 5, 19, 14, 0)
    fake_conn.fetchrow.return_value = _ctx_row()
    fake_chatwoot.assign_team.side_effect = ChatwootStructuralError(
        "Team not found", status_code=404
    )

    result = await initiate_handoff(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        team_id_str="3",
        private_note="note",
        last_reply=None,
        intent_tier=None,
        customer_name=None,
        requested_at=datetime.datetime.now(UTC),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
        meta_client=fake_meta,
    )

    assert result.outcome == "errored"
    assert "chatwoot_4xx" in (result.reason or "")


@pytest.mark.asyncio
async def test_handoff_non_numeric_team_id_errored_early(
    freeze_now, fake_conn, fake_chatwoot, fake_meta, fake_secrets_get
) -> None:
    freeze_now.set(2026, 5, 19, 14, 0)
    fake_conn.fetchrow.return_value = _ctx_row()

    result = await initiate_handoff(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        team_id_str="not-a-number",
        private_note="note",
        last_reply=None,
        intent_tier=None,
        customer_name=None,
        requested_at=datetime.datetime.now(UTC),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
        meta_client=fake_meta,
    )

    assert result.outcome == "errored"
    assert "team_id_not_numeric" in (result.reason or "")
    # No outbound calls fired.
    fake_meta.send_text.assert_not_awaited()
    fake_chatwoot.flip_conversation_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_conversation_not_found_errored(
    fake_conn, fake_chatwoot, fake_meta, fake_secrets_get
) -> None:
    fake_conn.fetchrow.return_value = None

    result = await initiate_handoff(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        team_id_str="3",
        private_note="note",
        last_reply=None,
        intent_tier=None,
        customer_name=None,
        requested_at=datetime.datetime.now(UTC),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
        meta_client=fake_meta,
    )
    assert result.outcome == "errored"
    assert result.reason == "conversation_not_found"


@pytest.mark.asyncio
async def test_handoff_chatwoot_not_linked_errored(
    fake_conn, fake_chatwoot, fake_meta, fake_secrets_get
) -> None:
    fake_conn.fetchrow.return_value = _ctx_row(chatwoot_account_id=None)  # type: ignore[arg-type]

    result = await initiate_handoff(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        team_id_str="3",
        private_note="note",
        last_reply=None,
        intent_tier=None,
        customer_name=None,
        requested_at=datetime.datetime.now(UTC),
        conn=fake_conn,
        chatwoot=fake_chatwoot,
        meta_client=fake_meta,
    )
    assert result.outcome == "errored"
    assert "chatwoot_not_linked" in (result.reason or "")
