"""Phase 2 PR-7 — Chatwoot router gap-fix tests (DB-free, mocked).

Covers:
  A1 — attachments[] extraction across file_type values
  A4 — in_reply_to extraction from content_attributes
  B3/B4 — 500 on Redis / Postgres outage (retryable by the CW agent-bot webhook)
  D1 — PROCEED uses DECISION_TO_EVENT_STATUS mapping (regression guard)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import fakeredis.aioredis
import pytest
import pytest_asyncio
import redis.exceptions as redis_exc
from httpx import ASGITransport, AsyncClient
from polaris_api.config import get_settings
from polaris_api.main import app
from polaris_api.services import inbound_queue
from polaris_api.services.webhook_resolver import ResolvedChannel
from polaris_api.webhooks import chatwoot_router as chatwoot_router_module

# Pin loop_scope=function for every async test + fixture in this file. These
# tests build their own AsyncClient against the app; without the pin, a
# function-scope teardown can close the session event loop that conftest's
# session-scoped async fixtures hold, making async tests that run AFTER an
# offending file hang / fail spuriously. See conftest.py's "Known issue —
# function-scoped admin test fixtures + session event loop" note and the same
# pin in test_admin_chunking_config.py / test_admin_force_run.py.
pytestmark = pytest.mark.asyncio(loop_scope="function")

_FAKE_SECRET = "phase-2-pr-7-chatwoot-secret"


def _sign(raw: bytes) -> str:
    return hmac.new(_FAKE_SECRET.encode(), raw, hashlib.sha256).hexdigest()


@pytest.fixture
def configured_settings() -> Iterator[None]:
    get_settings.cache_clear()
    s = get_settings()
    s.chatwoot_webhook_hmac_secret = _FAKE_SECRET
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def mock_pipeline(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    recorder = AsyncMock(return_value=uuid4())
    resolved_channel = ResolvedChannel(
        tenant_id=uuid4(),
        channel_id=uuid4(),
        channel_kind="whatsapp",
        chatwoot_inbox_id=42,
    )
    resolver = AsyncMock(return_value=resolved_channel)
    # The incoming hot path now resolves channel + tenant status in ONE call
    # (resolve_chatwoot_with_status). resolve_chatwoot is still patched for the
    # human-agent-reply path which uses it; fetch_tenant_status is no longer
    # referenced by the router, so it is not patched here.
    combined_resolver = AsyncMock(return_value=(resolved_channel, "active", None))
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(chatwoot_router_module, "record_event", recorder)
    monkeypatch.setattr(chatwoot_router_module, "resolve_chatwoot", resolver)
    monkeypatch.setattr(
        chatwoot_router_module, "resolve_chatwoot_with_status", combined_resolver
    )
    monkeypatch.setattr(chatwoot_router_module, "get_redis", lambda: fake)
    inbound_queue.reset_queue_for_tests()
    return recorder


@pytest_asyncio.fixture(loop_scope="function")
async def chatwoot_client(
    configured_settings: None, mock_pipeline: AsyncMock
) -> AsyncIterator[AsyncClient]:
    _ = configured_settings
    _ = mock_pipeline
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    inbound_queue.reset_queue_for_tests()


def _payload(**overrides) -> bytes:
    base = {
        "event": "message_created",
        "id": 12345,
        "account": {"id": 1},
        "inbox": {"id": 7},
        # The Chatwoot-native rehaul added two gates to the incoming path that
        # this PR-7-era payload predates, so the default conversation must
        # satisfy both for the message to reach the enqueue (PROCEED) path:
        #   1. is_bot_owned() — status must be 'pending' (open/resolved/snoozed
        #      = a human owns it → dropped as human_handling).
        #   2. extract_chatwoot_recipient() — a real reply recipient must
        #      resolve from contact_inbox.source_id (else dropped as
        #      no_recipient). For whatsapp that's the wa_id.
        # (Both gates themselves are covered in test_chatwoot_native_gate.py.)
        "conversation": {
            "id": 99,
            "status": "pending",
            "contact_inbox": {"source_id": "60123456789"},
        },
        "sender": {"id": 42, "name": "Jane"},
        "content": "hello",
        "message_type": "incoming",
        "created_at": "2026-05-17T08:00:00Z",
    }
    base.update(overrides)
    return json.dumps(base).encode("utf-8")


async def test_attachments_extracted_per_file_type(
    chatwoot_client: AsyncClient, mock_pipeline: AsyncMock
) -> None:
    body = _payload(
        id=10001,
        attachments=[
            {"id": 1, "file_type": "image", "data_url": "https://cw/img.jpg"},
            {"id": 2, "file_type": "audio", "data_url": "https://cw/voice.m4a"},
            {"id": 3, "file_type": "video", "data_url": "https://cw/clip.mp4"},
            {"id": 4, "file_type": "file",  "data_url": "https://cw/doc.pdf"},
        ],
    )
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    queue = inbound_queue.get_queue()
    assert queue.qsize() == 1
    msg = await queue.get()
    queue.task_done()

    kinds = [a.kind for a in msg.attachments]
    assert kinds == ["image", "audio", "video", "document"]
    urls = [a.url for a in msg.attachments]
    assert urls == [
        "https://cw/img.jpg",
        "https://cw/voice.m4a",
        "https://cw/clip.mp4",
        "https://cw/doc.pdf",
    ]
    assert msg.attachments[3].media_id == "4"


async def test_unsupported_file_type_skipped(
    chatwoot_client: AsyncClient, mock_pipeline: AsyncMock
) -> None:
    """Chatwoot may add new file_type values in future versions. Unknown
    values must be skipped silently, not crash."""
    body = _payload(
        id=10002,
        attachments=[
            {"id": 1, "file_type": "image", "data_url": "https://cw/x.jpg"},
            {"id": 2, "file_type": "future_unknown_type", "data_url": "https://cw/?"},
        ],
    )
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    queue = inbound_queue.get_queue()
    msg = await queue.get()
    queue.task_done()
    assert len(msg.attachments) == 1
    assert msg.attachments[0].kind == "image"


async def test_reply_to_in_content_attributes_extracts_external_id(
    chatwoot_client: AsyncClient, mock_pipeline: AsyncMock
) -> None:
    """Relay-initiated reply: the wamid is in content_attributes.in_reply_to_external_id."""
    body = _payload(
        id=10003,
        content_attributes={"in_reply_to_external_id": "wamid.abc-xyz"},
    )
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    queue = inbound_queue.get_queue()
    msg = await queue.get()
    queue.task_done()
    assert msg.in_reply_to == "wamid.abc-xyz"


async def test_reply_to_as_numeric_chatwoot_id_stringified(
    chatwoot_client: AsyncClient, mock_pipeline: AsyncMock
) -> None:
    """UI-initiated reply: content_attributes.in_reply_to is a numeric
    chatwoot message id; we stringify as 'chatwoot.<id>'."""
    body = _payload(
        id=10004,
        content_attributes={"in_reply_to": 555},
    )
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    queue = inbound_queue.get_queue()
    msg = await queue.get()
    queue.task_done()
    assert msg.in_reply_to == "chatwoot.555"


async def test_no_reply_yields_none(
    chatwoot_client: AsyncClient, mock_pipeline: AsyncMock
) -> None:
    body = _payload(id=10005)
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    queue = inbound_queue.get_queue()
    msg = await queue.get()
    queue.task_done()
    assert msg.in_reply_to is None


async def test_redis_outage_returns_500(
    chatwoot_client: AsyncClient,
    mock_pipeline: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Webhook-resilience: a transient backend outage now returns 500 (not 503)
    # so the Chatwoot agent-bot webhook RETRIES (500 is in
    # RETRYABLE_AGENT_BOT_STATUSES) instead of evicting the conversation from
    # bot mode. Either way Chatwoot is told to retry; 500 is retryable on the
    # fork regardless of the 502/503/504 addition.
    class _FailingRedis:
        async def set(self, *args, **kwargs) -> None:
            raise redis_exc.ConnectionError("redis down")

    monkeypatch.setattr(chatwoot_router_module, "get_redis", lambda: _FailingRedis())
    body = _payload(id=10006)
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 500


async def test_postgres_outage_returns_500(
    chatwoot_client: AsyncClient,
    mock_pipeline: AsyncMock,
) -> None:
    mock_pipeline.side_effect = asyncpg.PostgresError("db down")
    body = _payload(id=10007)
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 500


async def test_received_status_uses_decision_mapping(
    chatwoot_client: AsyncClient, mock_pipeline: AsyncMock
) -> None:
    """D1 regression guard: PROCEED → 'received' via the shared mapping.
    If a future refactor changes the mapping, this test fails (vs the
    previous hardcoded 'received' which would silently desync)."""
    body = _payload(id=10008)
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json", "X-Chatwoot-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    # The 'received' call is the last one (after the SETNX claim).
    received_calls = [
        c for c in mock_pipeline.await_args_list
        if c.kwargs.get("status") == "received"
    ]
    assert len(received_calls) == 1
