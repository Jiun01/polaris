"""Unit tests for the demo runtime decision state machine — ADR-066.

resolve() is read-only; we monkeypatch the demo_sessions primitives (AsyncMock)
so the decision logic is tested without a DB. Covers: default, activate, refuse
(one-ever lock), continue, expire-by-cap, expire-by-time, no mid-demo switch,
and exact whole-token matching.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from polaris_api.services import demo_runtime as dr
from polaris_api.services import demo_sessions as ds

pytestmark = pytest.mark.asyncio(loop_scope="function")

_GATEWAY = ds.POLARIS_INTERNAL_TENANT_ID
_BRAND = UUID("11111111-1111-1111-1111-111111111111")

_DEMO = ds.DemoTenant(
    tenant_id=_BRAND,
    slug="trg-watch",
    display_name="TRG Watch",
    demo_code="trgwatches1824",
    duration_hours=6,
    max_messages=50,
)


def _active(*, count: int, max_messages: int = 50, expires_in_h: float = 5.0) -> ds.ActiveDemoSession:
    return ds.ActiveDemoSession(
        session_id=UUID("22222222-2222-2222-2222-222222222222"),
        tenant_id=_BRAND,
        demo_code="trgwatches1824",
        message_count=count,
        max_messages=max_messages,
        expires_at=datetime.now(UTC) + timedelta(hours=expires_in_h),
        chatwoot_conversation_id=99,
        chatwoot_inbox_id=3,
    )


def _patch(monkeypatch: pytest.MonkeyPatch, *, active, found=None, used=False) -> dict:
    get_active = AsyncMock(return_value=active)
    has_used = AsyncMock(return_value=used)

    async def _find(token: str):
        return _DEMO if (found is not None and token == found) else None

    find = AsyncMock(side_effect=_find)
    monkeypatch.setattr(ds, "get_active_demo_session", get_active)
    monkeypatch.setattr(ds, "find_demo_tenant_by_code", find)
    monkeypatch.setattr(ds, "has_ever_used_demo", has_used)
    return {"find": find, "has_used": has_used}


# --- tokenizer (exact whole-token) -------------------------------------------

async def test_tokens_whole_token_only() -> None:
    assert dr._tokens("hi trgwatches1824!") == ["hi", "trgwatches1824"]
    assert dr._tokens("code:trgwatches1824 please") == ["code", "trgwatches1824", "please"]
    # substring must NOT surface as the code token
    assert "trgwatches1824" not in dr._tokens("mytrgwatches1824x")


# --- DEFAULT -----------------------------------------------------------------

async def test_default_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, active=None, found=None)
    d = await dr.resolve(body="hello there", channel_user_id="+60123", gateway_tenant_id=_GATEWAY)
    assert d.mode == "default"
    assert d.content_tenant_id == _GATEWAY
    assert d.brand_demo is False


async def test_default_substring_is_not_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    # find only matches the exact token "trgwatches1824"
    _patch(monkeypatch, active=None, found="trgwatches1824")
    d = await dr.resolve(body="mytrgwatches1824x", channel_user_id="+60123", gateway_tenant_id=_GATEWAY)
    assert d.mode == "default"


# --- ACTIVATE ----------------------------------------------------------------

async def test_activate_unused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, active=None, found="trgwatches1824", used=False)
    d = await dr.resolve(body="hi TRGWATCHES1824", channel_user_id="+60123", gateway_tenant_id=_GATEWAY)
    assert d.mode == "activate"
    assert d.content_tenant_id == _BRAND
    assert d.brand_demo is True
    assert d.demo is _DEMO
    assert d.direct_message is not None
    assert "TRG Watch" in d.direct_message
    assert "6 hours" in d.direct_message and "50 messages" in d.direct_message


# --- REFUSE (one demo ever) --------------------------------------------------

async def test_refuse_already_used(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, active=None, found="trgwatches1824", used=True)
    d = await dr.resolve(body="trgwatches1824", channel_user_id="+60123", gateway_tenant_id=_GATEWAY)
    assert d.mode == "refuse"
    assert d.content_tenant_id == _GATEWAY
    assert d.brand_demo is False
    assert d.direct_message is not None
    assert "already used" in d.direct_message.lower()


# --- CONTINUE + no mid-demo switch -------------------------------------------

async def test_continue_demo_ignores_other_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    spies = _patch(monkeypatch, active=_active(count=3), found="nottipets2156")
    d = await dr.resolve(body="nottipets2156", channel_user_id="+60123", gateway_tenant_id=_GATEWAY)
    assert d.mode == "continue_demo"
    assert d.content_tenant_id == _BRAND
    assert d.brand_demo is True
    # codes are not even looked up while a demo is active
    spies["find"].assert_not_called()


# --- EXPIRE ------------------------------------------------------------------

async def test_expire_by_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, active=_active(count=50, max_messages=50))
    d = await dr.resolve(body="more please", channel_user_id="+60123", gateway_tenant_id=_GATEWAY)
    assert d.mode == "expire"
    assert d.content_tenant_id == _GATEWAY
    assert d.brand_demo is False
    assert d.session is not None
    assert d.direct_message is not None and "ended" in d.direct_message.lower()


async def test_expire_by_time(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, active=_active(count=2, expires_in_h=-1.0))
    d = await dr.resolve(body="still there?", channel_user_id="+60123", gateway_tenant_id=_GATEWAY)
    assert d.mode == "expire"
    assert d.session is not None
