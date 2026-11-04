"""Phase 8 PR-3 — tenant_dashboard router smoke tests.

The Dashboard endpoint is an aggregate read across 6+ tables (tenants,
tenant_quotas, conversations, broadcasts, tenant_promotions,
ingestion_requests, messages). Full integration coverage is DB-gated
and lands when those phases' fixtures merge; this file provides
in-process unit coverage of the pure-Python helpers + shape contracts
so collection works in CI without `POLARIS_DATABASE_URL`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from polaris_api.routers.tenant_dashboard import _fill_sparkline, _row_to_recent
from polaris_shared_types.portal_dashboard import DashboardRecentConversation


class _FakeRow:
    """Minimal asyncpg.Record stand-in for unit tests."""

    def __init__(self, day: object, replies: int) -> None:
        self._d = {"day": day, "replies": replies}

    def __getitem__(self, key: str) -> object:
        return self._d[key]


def test_fill_sparkline_pads_missing_days_with_zero() -> None:
    """A tenant with replies only today should produce [0,0,0,0,0,0,N]."""
    today = datetime.now(UTC).date()
    rows = [_FakeRow(today, 17)]
    out = _fill_sparkline(rows, days=7)  # type: ignore[arg-type]
    assert len(out) == 7
    assert out[:6] == [0, 0, 0, 0, 0, 0]
    assert out[6] == 17


def test_fill_sparkline_oldest_first_ordering() -> None:
    """Result is oldest-first so the portal sparkline draws left→right."""
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    rows = [_FakeRow(today, 5), _FakeRow(yesterday, 3)]
    out = _fill_sparkline(rows, days=3)  # type: ignore[arg-type]
    assert out == [0, 3, 5]


def test_fill_sparkline_drops_rows_outside_window() -> None:
    """A row 14 days old must NOT bleed into a 7-day sparkline."""
    today = datetime.now(UTC).date()
    fortnight_ago = today - timedelta(days=14)
    rows = [_FakeRow(today, 4), _FakeRow(fortnight_ago, 99)]
    out = _fill_sparkline(rows, days=7)  # type: ignore[arg-type]
    assert out[-1] == 4
    assert 99 not in out


def test_fill_sparkline_handles_empty() -> None:
    """No replies in the window → all zeros, correct length."""
    out = _fill_sparkline([], days=7)
    assert out == [0, 0, 0, 0, 0, 0, 0]


# ── channel = "telegram" tests ──────────────────────────────────────────────


def test_dashboard_recent_conversation_accepts_telegram_channel() -> None:
    """DashboardRecentConversation must not raise when channel='telegram'.

    Regression guard for the Pydantic ValidationError 500 that occurred when
    /api/tenant/dashboard/summary was called for a tenant with a Telegram
    conversation (channel Literal was too narrow: whatsapp/instagram/messenger).
    """
    model = DashboardRecentConversation(
        conversation_id=uuid.uuid4(),
        channel="telegram",
        customer_name="Test User",
        last_message_at=datetime.now(UTC).isoformat(),
        message_count=3,
        intent="faq",
        status="open",
    )
    assert model.channel == "telegram"


class _FakeTelegramRow:
    """Minimal asyncpg.Record stand-in simulating a telegram conversation row."""

    def __init__(self) -> None:
        self._d = {
            "conversation_id": uuid.uuid4(),
            "channel": "telegram",
            "customer_name": "Telegram User",
            "last_message_at": datetime.now(UTC),
            "message_count": 5,
            "intent": "handoff",
            "status": "open",
        }

    def __getitem__(self, key: str) -> object:
        return self._d[key]


def test_row_to_recent_accepts_telegram_channel() -> None:
    """_row_to_recent must map a telegram DB row to DashboardRecentConversation.

    Validates that the cast(ConversationChannel, r["channel"]) path in the
    router correctly constructs the model when channel='telegram'.
    """
    row = _FakeTelegramRow()
    result = _row_to_recent(row)  # type: ignore[arg-type]  # asyncpg.Record stand-in
    assert isinstance(result, DashboardRecentConversation)
    assert result.channel == "telegram"
    assert result.message_count == 5
