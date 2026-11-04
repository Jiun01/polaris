"""Broadcast compliance-gate tests — Phase 6 PR-6.

Pure unit coverage of ``services/broadcast/compliance.check``. The
function takes an asyncpg.Connection; we stub it with an AsyncMock
configured to return canned rows. Full DB integration runs in CI.

Three rules covered:
  - opted_out (with stop_keyword sub-detection)
  - 24h-window for IG/Messenger MARKETING templates
  - 24h-window bypass for WhatsApp + non-MARKETING categories
  - 24h-window env disable switch
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.broadcast import compliance


def _conn_returning(
    *, optout_row: dict | None, last_inbound: datetime | None = None
) -> AsyncMock:
    """Build an AsyncMock connection with canned fetchrow + fetchval."""
    conn = AsyncMock()
    conn.fetchrow.return_value = optout_row
    conn.fetchval.return_value = last_inbound
    return conn


@pytest.mark.asyncio
async def test_opted_out_with_stop_keyword_reason() -> None:
    conn = _conn_returning(
        optout_row={
            "opted_out_at": datetime(2026, 5, 1, tzinfo=UTC),
            "opted_out_reason": "stop_keyword",
        }
    )

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="+60123456789",
        template_category="MARKETING",
    )

    assert result.allowed is False
    assert result.skip_reason == "stop_keyword"


@pytest.mark.asyncio
async def test_opted_out_with_manual_reason() -> None:
    """Manual / dsar opt-outs surface as the generic 'opted_out'
    skip reason (the portal cares about stop_keyword specifically;
    everything else is just 'opted out')."""
    conn = _conn_returning(
        optout_row={
            "opted_out_at": datetime(2026, 5, 1, tzinfo=UTC),
            "opted_out_reason": "manual",
        }
    )

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="+60123456789",
        template_category="MARKETING",
    )

    assert result.allowed is False
    assert result.skip_reason == "opted_out"


@pytest.mark.asyncio
async def test_whatsapp_marketing_bypasses_24h_window() -> None:
    """WhatsApp templates ARE the window-reopen mechanism per Meta
    policy; even MARKETING category sends are allowed without an
    inbound in the last 24h."""
    conn = _conn_returning(
        optout_row=None,
        last_inbound=None,  # never inbound
    )

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="+60123456789",
        template_category="MARKETING",
    )

    assert result.allowed is True


@pytest.mark.asyncio
async def test_utility_template_bypasses_24h_window_on_ig() -> None:
    """UTILITY category bypasses the 24h-window on IG/Messenger
    per Meta policy. MARKETING is the only category gated."""
    conn = _conn_returning(optout_row=None, last_inbound=None)

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="messenger",
        channel_user_id="ig_user_abc",
        template_category="UTILITY",
    )

    assert result.allowed is True


@pytest.mark.asyncio
async def test_ig_marketing_with_no_inbound_skips_window_closed() -> None:
    conn = _conn_returning(optout_row=None, last_inbound=None)

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="messenger",
        channel_user_id="ig_user_abc",
        template_category="MARKETING",
    )

    assert result.allowed is False
    assert result.skip_reason == "window_closed"


@pytest.mark.asyncio
async def test_ig_marketing_within_24h_allowed() -> None:
    now = datetime(2026, 5, 19, 14, 0, tzinfo=UTC)
    recent_inbound = now - timedelta(hours=2)
    conn = _conn_returning(optout_row=None, last_inbound=recent_inbound)

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="messenger",
        channel_user_id="ig_user_abc",
        template_category="MARKETING",
        now=now,
    )

    assert result.allowed is True


@pytest.mark.asyncio
async def test_messenger_marketing_outside_24h_skipped() -> None:
    now = datetime(2026, 5, 19, 14, 0, tzinfo=UTC)
    stale_inbound = now - timedelta(hours=25)
    conn = _conn_returning(optout_row=None, last_inbound=stale_inbound)

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="messenger",
        channel_user_id="fb_psid_xyz",
        template_category="MARKETING",
        now=now,
    )

    assert result.allowed is False
    assert result.skip_reason == "window_closed"


@pytest.mark.asyncio
async def test_window_check_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BROADCAST_24H_WINDOW_CHECK_ENABLED=false lifts rule 3 for
    sandbox testing — disabling in prod is a Meta-policy violation,
    documented in the env-var description."""
    monkeypatch.setenv("BROADCAST_24H_WINDOW_CHECK_ENABLED", "false")
    conn = _conn_returning(optout_row=None, last_inbound=None)

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="messenger",
        channel_user_id="ig_user_abc",
        template_category="MARKETING",
    )

    assert result.allowed is True


@pytest.mark.asyncio
async def test_window_check_typo_in_env_keeps_check_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything other than literal 'false' keeps the check on. A typo
    like 'fakse' would otherwise silently disable a compliance rule —
    we're deliberately strict on this env var."""
    monkeypatch.setenv("BROADCAST_24H_WINDOW_CHECK_ENABLED", "fakse")
    conn = _conn_returning(optout_row=None, last_inbound=None)

    result = await compliance.check(
        conn,
        tenant_id=uuid4(),
        channel="messenger",
        channel_user_id="ig_user_abc",
        template_category="MARKETING",
    )

    assert result.allowed is False
    assert result.skip_reason == "window_closed"


def test_window_check_enabled_default_is_true() -> None:
    """Unset env -> check enabled. Pin the default so a future
    refactor of _window_check_enabled can't accidentally invert it."""
    # Defensive: clear env if leftover from another test.
    os.environ.pop("BROADCAST_24H_WINDOW_CHECK_ENABLED", None)
    assert compliance._window_check_enabled() is True  # noqa: SLF001
