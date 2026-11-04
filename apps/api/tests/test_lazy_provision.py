"""Lazy channel provisioning on first inbound — unit + router-branch tests.

Covers ``services/channels/lazy_provision.lazy_provision_channel`` outcomes and
the ``chatwoot_router`` wiring (negative cache + skip-on-disallowed).

Fully mocked — no DB / no Chatwoot. Fast.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="function")

os.environ.setdefault("JWT_SECRET", "pytest-secret-32-bytes-padded-xxxxxxxxxx")
os.environ.setdefault("JWT_ISSUER", "polaris-portal")
os.environ.setdefault(
    "POLARIS_KMS_KEY",
    "00112233445566778899aabbccddeeff" "ffeeddccbbaa99887766554433221100",
)

from polaris_api.services.channels import lazy_provision as lp  # noqa: E402
from polaris_api.services.webhook_resolver import (  # noqa: E402
    ResolvedChannel,
    ResolvedTenant,
)

_MOD = "polaris_api.services.channels.lazy_provision"


# --------------------------------------------------------------- fakes ----


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, allowed: list[str] | None) -> None:
        self._allowed = allowed

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "allowed_channel_kinds" in sql:
            if self._allowed is None:
                return None
            return {"allowed_channel_kinds": self._allowed}
        return None


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _inbox(channel_type: str, inbox_id: int = 7) -> dict[str, Any]:
    return {
        "id": inbox_id,
        "channel_type": channel_type,
        "name": "Test inbox",
        "page_id": "FB_PAGE_1",
    }


def _patch_common(
    *,
    tenant_id: Any,
    allowed: list[str] | None,
    inboxes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Patch the lazy_provision collaborators; return the mock handles."""
    pool = _FakePool(_FakeConn(allowed))
    cw = MagicMock()
    cw.list_inboxes = AsyncMock(return_value=inboxes)
    cw.get_channel_token = AsyncMock(return_value="tok-abc")

    patches = {
        "resolve_by_chatwoot_account_id": patch(
            f"{_MOD}.resolve_by_chatwoot_account_id",
            AsyncMock(
                return_value=ResolvedTenant(
                    tenant_id=tenant_id, chatwoot_account_id=5
                )
            ),
        ),
        "get_chatwoot_client": patch(
            f"{_MOD}.get_chatwoot_client", MagicMock(return_value=cw)
        ),
        "get_service_pool": patch(
            f"{_MOD}.get_service_pool", MagicMock(return_value=pool)
        ),
        "provision_channel": patch(
            f"{_MOD}.provision_channel", AsyncMock(return_value=None)
        ),
        "audit_record": patch(f"{_MOD}.audit_record", AsyncMock()),
        "attach_inbox_agent_bot": patch(
            f"{_MOD}.attach_inbox_agent_bot", AsyncMock(return_value=True)
        ),
        "resolve_chatwoot_with_status": patch(
            f"{_MOD}.resolve_chatwoot_with_status",
            AsyncMock(
                return_value=(
                    ResolvedChannel(
                        tenant_id=tenant_id,
                        channel_id=uuid4(),
                        channel_kind="messenger",
                        chatwoot_inbox_id=7,
                    ),
                    "active",
                    None,
                )
            ),
        ),
    }
    secrets_put = patch(f"{_MOD}.secrets_svc.put", AsyncMock())
    handles = {k: p.start() for k, p in patches.items()}
    handles["secrets_put"] = secrets_put.start()
    return handles


# ----------------------------------------------------- lazy_provision ----


async def test_entitled_provisionable_provisions() -> None:
    tenant_id = uuid4()
    h = _patch_common(
        tenant_id=tenant_id,
        allowed=["whatsapp", "messenger", "instagram_login"],
        inboxes=[_inbox("Channel::FacebookPage")],
    )
    try:
        result = await lp.lazy_provision_channel(account_id=5, inbox_id=7)
    finally:
        patch.stopall()

    assert result.outcome == "provisioned"
    assert result.channel_kind == "messenger"
    assert result.resolved is not None
    # provision_channel called WITHOUT bypass (entitlement is authoritative).
    _, kwargs = h["provision_channel"].call_args
    assert kwargs["bypass_tier_cap"] is False
    assert kwargs["channel_kind"] == "messenger"


async def test_disallowed_kind_skips() -> None:
    tenant_id = uuid4()
    h = _patch_common(
        tenant_id=tenant_id,
        allowed=["whatsapp"],  # messenger NOT entitled
        inboxes=[_inbox("Channel::FacebookPage")],
    )
    try:
        result = await lp.lazy_provision_channel(account_id=5, inbox_id=7)
    finally:
        patch.stopall()

    assert result.outcome == "disallowed"
    assert result.channel_kind == "messenger"
    h["provision_channel"].assert_not_called()


async def test_no_tenant_for_account() -> None:
    with patch(
        f"{_MOD}.resolve_by_chatwoot_account_id", AsyncMock(return_value=None)
    ):
        result = await lp.lazy_provision_channel(account_id=999, inbox_id=7)
    assert result.outcome == "no_tenant"


async def test_telegram_out_of_scope() -> None:
    tenant_id = uuid4()
    _patch_common(
        tenant_id=tenant_id,
        allowed=["whatsapp", "telegram"],
        inboxes=[_inbox("Channel::Telegram")],
    )
    try:
        result = await lp.lazy_provision_channel(account_id=5, inbox_id=7)
    finally:
        patch.stopall()
    assert result.outcome == "not_applicable"
    assert result.channel_kind == "telegram"


async def test_inbox_not_found_is_not_applicable() -> None:
    tenant_id = uuid4()
    _patch_common(
        tenant_id=tenant_id,
        allowed=["messenger"],
        inboxes=[_inbox("Channel::FacebookPage", inbox_id=99)],  # not 7
    )
    try:
        result = await lp.lazy_provision_channel(account_id=5, inbox_id=7)
    finally:
        patch.stopall()
    assert result.outcome == "not_applicable"


async def test_unique_violation_race_treated_provisioned() -> None:
    tenant_id = uuid4()
    h = _patch_common(
        tenant_id=tenant_id,
        allowed=["messenger"],
        inboxes=[_inbox("Channel::FacebookPage")],
    )
    h["provision_channel"].side_effect = asyncpg.UniqueViolationError("dup")
    try:
        result = await lp.lazy_provision_channel(account_id=5, inbox_id=7)
    finally:
        patch.stopall()
    # A concurrent first message already provisioned — re-resolve, still success.
    assert result.outcome == "provisioned"


# ------------------------------------------------------ router wiring ----

from polaris_api.config import get_settings  # noqa: E402
from polaris_api.main import app  # noqa: E402
from polaris_api.webhooks import chatwoot_router as cr  # noqa: E402


def _msg_body() -> bytes:
    return json.dumps(
        {
            "event": "message_created",
            "message_type": "incoming",
            "id": 1,
            "account": {"id": 5},
            "inbox": {"id": 7},
            "conversation": {"id": 9},
            "sender": {"id": 42, "name": "Jane"},
            "content": "hello",
        }
    ).encode("utf-8")


async def test_router_disallowed_sets_cache_and_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    rec = AsyncMock()
    monkeypatch.setattr(cr, "verify_chatwoot_signature", lambda *a, **k: True)
    monkeypatch.setattr(cr, "get_redis", lambda: fake)
    monkeypatch.setattr(
        cr, "resolve_chatwoot_with_status", AsyncMock(return_value=(None, None, None))
    )
    monkeypatch.setattr(cr, "record_event", rec)
    monkeypatch.setattr(
        cr,
        "lazy_provision_channel",
        AsyncMock(
            return_value=lp.LazyProvisionResult(
                outcome="disallowed", channel_kind="messenger"
            )
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            "/v1/webhooks/chatwoot",
            content=_msg_body(),
            headers={
                "x-chatwoot-signature": "x",
                "x-chatwoot-timestamp": "1",
            },
        )

    assert res.status_code == 204, res.text
    assert await fake.get(cr._lazy_skip_key(5, 7)) is not None
    # error_class distinguishes the entitlement skip on the health page.
    _, kwargs = rec.call_args
    assert kwargs["error_class"].startswith("ChannelNotEntitled:")


async def test_router_cache_hit_skips_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    await fake.set(cr._lazy_skip_key(5, 7), b"1")
    lazy = AsyncMock()
    monkeypatch.setattr(cr, "verify_chatwoot_signature", lambda *a, **k: True)
    monkeypatch.setattr(cr, "get_redis", lambda: fake)
    monkeypatch.setattr(
        cr, "resolve_chatwoot_with_status", AsyncMock(return_value=(None, None, None))
    )
    monkeypatch.setattr(cr, "record_event", AsyncMock())
    monkeypatch.setattr(cr, "lazy_provision_channel", lazy)

    # Ensure the secret is configured so the (patched) verify path is reached.
    get_settings.cache_clear()
    get_settings().chatwoot_webhook_hmac_secret = "s"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            res = await client.post(
                "/v1/webhooks/chatwoot",
                content=_msg_body(),
                headers={
                    "x-chatwoot-signature": "x",
                    "x-chatwoot-timestamp": "1",
                },
            )
    finally:
        get_settings.cache_clear()

    assert res.status_code == 204, res.text
    lazy.assert_not_called()
