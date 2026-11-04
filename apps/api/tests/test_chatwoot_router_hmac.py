"""Phase 2 PR-5 — Chatwoot router HMAC + event-discriminator unit tests.

DB-free. Mocks the resolver + recorder + redis so the assertion stays
on the dispatcher's branching, not on the DB write side-effects (which
``test_chatwoot_router.py`` exercises against a real DB).

The Polaris Chatwoot fork signs ``X-Chatwoot-Signature: sha256=<hex>`` where
``hex = HMAC-SHA256(secret, "{X-Chatwoot-Timestamp}.{raw_body}")`` — a
timestamp-prefixed scheme with replay protection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from polaris_api.config import get_settings
from polaris_api.main import app
from polaris_api.services import inbound_queue
from polaris_api.services.channels.lazy_provision import LazyProvisionResult
from polaris_api.webhooks import chatwoot_router as chatwoot_router_module

_FAKE_SECRET = "phase-2-pr-5-chatwoot-test-secret"


def _sign(
    raw: bytes,
    secret: str = _FAKE_SECRET,
    timestamp: str | None = None,
    *,
    prefix: bool = True,
) -> str:
    """Sign the fork's way: ``sha256=`` + HMAC over ``"{ts}.{body}"``.

    When ``timestamp`` is None, fall back to bare-hex over the raw body (the
    legacy scheme) so back-compat can be exercised.
    """
    msg = f"{timestamp}.".encode() + raw if timestamp is not None else raw
    hex_digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"sha256={hex_digest}" if prefix else hex_digest


def _fresh_ts() -> str:
    return str(int(time.time()))


@pytest.fixture
def configured_settings() -> Iterator[None]:
    get_settings.cache_clear()
    settings = get_settings()
    settings.chatwoot_webhook_hmac_secret = _FAKE_SECRET
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def mocked_chatwoot_deps(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace record_event + resolver + redis with mocks so the
    dispatcher's branching can be tested DB-free."""
    recorder = AsyncMock(return_value=uuid4())
    resolver = AsyncMock(return_value=None)
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(chatwoot_router_module, "record_event", recorder)
    monkeypatch.setattr(chatwoot_router_module, "resolve_chatwoot", resolver)
    # Incoming hot path resolves channel + tenant status in one call. None →
    # tenant_not_found (the default for these HMAC-branch tests, which mostly
    # never reach resolution).
    monkeypatch.setattr(
        chatwoot_router_module,
        "resolve_chatwoot_with_status",
        AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(chatwoot_router_module, "get_redis", lambda: fake)
    # Lazy provisioning fires at the unknown-inbox dead-end; mock it to a no-op
    # ("no_tenant") so these DB-free HMAC-branch tests keep the original
    # tenant_not_found fall-through without touching the (uninitialised) pool.
    monkeypatch.setattr(
        chatwoot_router_module,
        "lazy_provision_channel",
        AsyncMock(return_value=LazyProvisionResult(outcome="no_tenant")),
    )
    inbound_queue.reset_queue_for_tests()
    return recorder


@pytest_asyncio.fixture
async def chatwoot_client(
    configured_settings: None,
    mocked_chatwoot_deps: AsyncMock,
) -> AsyncIterator[AsyncClient]:
    _ = configured_settings
    _ = mocked_chatwoot_deps
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    inbound_queue.reset_queue_for_tests()


def _sample_payload(
    event: str = "message_created",
    message_type: str = "incoming",
    inbox_id: int = 42,
    msg_id: int = 12345,
) -> bytes:
    return json.dumps(
        {
            "event": event,
            "id": msg_id,
            "account": {"id": 1},
            "inbox": {"id": inbox_id},
            "conversation": {"id": 99},
            "sender": {"id": 7, "name": "Jane"},
            "content": "hello from chatwoot",
            "message_type": message_type,
            "created_at": "2026-05-17T08:00:00Z",
        }
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_missing_signature_returns_401(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    body = _sample_payload()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert mocked_chatwoot_deps.await_count == 1
    assert mocked_chatwoot_deps.await_args.kwargs["status"] == "hmac_fail"


@pytest.mark.asyncio
async def test_wrong_signature_returns_401(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    body = _sample_payload()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, secret="not-the-real-secret"),
        },
    )
    assert resp.status_code == 401
    assert mocked_chatwoot_deps.await_count == 1
    assert mocked_chatwoot_deps.await_args.kwargs["status"] == "hmac_fail"


@pytest.mark.asyncio
async def test_valid_signature_with_timestamp_passes(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    """The fork's contract: ``sha256=`` prefix + HMAC over ``{ts}.{body}``
    with a fresh timestamp header → authenticated → routed (here a
    conversation lifecycle event 200s without a recorder call)."""
    body = _sample_payload(event="conversation_status_changed")
    ts = _fresh_ts()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=ts),
            "X-Chatwoot-Timestamp": ts,
        },
    )
    assert resp.status_code == 200
    assert mocked_chatwoot_deps.await_count == 0  # authenticated, no hmac_fail row


@pytest.mark.asyncio
async def test_sha256_prefix_handled(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    """The ``sha256=`` prefix must be stripped before compare — a request
    signed correctly with the prefix authenticates."""
    body = _sample_payload(inbox_id=99999)
    ts = _fresh_ts()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=ts, prefix=True),
            "X-Chatwoot-Timestamp": ts,
        },
    )
    # Authenticated → resolver returns None → 204 tenant_not_found (NOT 401).
    assert resp.status_code == 204
    assert mocked_chatwoot_deps.await_args.kwargs["status"] == "tenant_not_found"


@pytest.mark.asyncio
async def test_stale_timestamp_rejected(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    """A correctly-signed payload with a timestamp older than the skew
    window is rejected (replay protection)."""
    body = _sample_payload()
    stale_ts = str(int(time.time()) - 10_000)  # well beyond 300s skew
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=stale_ts),
            "X-Chatwoot-Timestamp": stale_ts,
        },
    )
    assert resp.status_code == 401
    assert mocked_chatwoot_deps.await_count == 1
    assert mocked_chatwoot_deps.await_args.kwargs["status"] == "hmac_fail"


@pytest.mark.asyncio
async def test_non_message_event_ignored(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    """Conversation lifecycle events land in Phase 7. Phase 2 returns
    200 to keep Chatwoot's delivery dashboard happy."""
    body = _sample_payload(event="conversation_status_changed")
    ts = _fresh_ts()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=ts),
            "X-Chatwoot-Timestamp": ts,
        },
    )
    assert resp.status_code == 200
    assert mocked_chatwoot_deps.await_count == 0  # no recorder call


@pytest.mark.asyncio
async def test_outgoing_message_ignored(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    """Outgoing messages would echo into the LLM pipeline as fresh
    user input — reject early."""
    body = _sample_payload(message_type="outgoing")
    ts = _fresh_ts()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=ts),
            "X-Chatwoot-Timestamp": ts,
        },
    )
    assert resp.status_code == 200
    assert mocked_chatwoot_deps.await_count == 0


@pytest.mark.asyncio
async def test_unknown_inbox_returns_204(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    """Resolver returns None (no tenant_channels row) → 204 +
    tenant_not_found row."""
    body = _sample_payload(inbox_id=99999)
    ts = _fresh_ts()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=ts),
            "X-Chatwoot-Timestamp": ts,
        },
    )
    assert resp.status_code == 204
    assert mocked_chatwoot_deps.await_count == 1
    assert mocked_chatwoot_deps.await_args.kwargs["status"] == "tenant_not_found"


@pytest.mark.asyncio
async def test_missing_inbox_id_returns_204(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    body = json.dumps(
        {
            "event": "message_created",
            "id": 1,
            "inbox": {},  # missing id
            "message_type": "incoming",
            "content": "x",
        }
    ).encode("utf-8")
    ts = _fresh_ts()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=ts),
            "X-Chatwoot-Timestamp": ts,
        },
    )
    assert resp.status_code == 204
    assert mocked_chatwoot_deps.await_count == 1
    assert mocked_chatwoot_deps.await_args.kwargs["error_class"] == "MissingInboxId"


@pytest.mark.asyncio
async def test_empty_chatwoot_secret_fails_closed(
    chatwoot_client: AsyncClient, mocked_chatwoot_deps: AsyncMock
) -> None:
    """Misconfigured deploy with empty CHATWOOT_WEBHOOK_HMAC_SECRET
    must NOT validate any request."""
    settings = get_settings()
    settings.chatwoot_webhook_hmac_secret = ""
    body = _sample_payload()
    ts = _fresh_ts()
    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body, timestamp=ts),
            "X-Chatwoot-Timestamp": ts,
        },
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Sub-fix C — email-assist queue-full must be RECOVERABLE, not silently lost.
# When the queue drops the job, the router must NOT finalize the idempotency
# claim or record PROCEED — it releases the claim + 503s so Chatwoot's retry
# re-processes the email (otherwise: customer email with no draft, no retry).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_queue_full_releases_claim_and_503s(
    configured_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from polaris_api.services.webhook_resolver import ResolvedChannel

    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    recorder = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(chatwoot_router_module, "record_event", recorder)
    monkeypatch.setattr(
        chatwoot_router_module,
        "resolve_chatwoot_with_status",
        AsyncMock(
            return_value=(
                ResolvedChannel(
                    tenant_id=uuid4(), channel_id=uuid4(),
                    channel_kind="email", chatwoot_inbox_id=42,
                ),
                "active",
                None,
            )
        ),
    )
    monkeypatch.setattr(chatwoot_router_module, "get_redis", lambda: fake)
    # Simulate a full queue: the job is dropped.
    monkeypatch.setattr(
        chatwoot_router_module.email_assist_queue,
        "enqueue",
        AsyncMock(return_value=False),
    )

    msg_id = 555
    body = _sample_payload(msg_id=msg_id)
    ts = _fresh_ts()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/webhooks/chatwoot",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Chatwoot-Signature": _sign(body, timestamp=ts),
                "X-Chatwoot-Timestamp": ts,
            },
        )

    assert resp.status_code == 503  # Chatwoot will retry
    # Idempotency claim released → the retry re-processes (not replay_ignored).
    assert await fake.get(f"polaris:webhook:dedup:chatwoot:chatwoot.{msg_id}") is None
    # PROCEED was never finalized for the dropped job.
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_queue_ok_records_proceed_and_keeps_claim(
    configured_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: a successful enqueue finalizes PROCEED and keeps the claim."""
    from polaris_api.services.webhook_resolver import ResolvedChannel

    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    recorder = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(chatwoot_router_module, "record_event", recorder)
    monkeypatch.setattr(
        chatwoot_router_module,
        "resolve_chatwoot_with_status",
        AsyncMock(
            return_value=(
                ResolvedChannel(
                    tenant_id=uuid4(), channel_id=uuid4(),
                    channel_kind="email", chatwoot_inbox_id=42,
                ),
                "active",
                None,
            )
        ),
    )
    monkeypatch.setattr(chatwoot_router_module, "get_redis", lambda: fake)
    monkeypatch.setattr(
        chatwoot_router_module.email_assist_queue,
        "enqueue",
        AsyncMock(return_value=True),
    )

    msg_id = 556
    body = _sample_payload(msg_id=msg_id)
    ts = _fresh_ts()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/webhooks/chatwoot",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Chatwoot-Signature": _sign(body, timestamp=ts),
                "X-Chatwoot-Timestamp": ts,
            },
        )

    assert resp.status_code == 200
    assert await fake.get(f"polaris:webhook:dedup:chatwoot:chatwoot.{msg_id}") is not None
    assert recorder.await_args.kwargs["status"] == "received"


@pytest.mark.asyncio
async def test_idempotency_release_round_trip() -> None:
    """release() deletes the key so a re-claim of the same delivery succeeds."""
    from polaris_api.services import idempotency

    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    assert await idempotency.claim(fake, "chatwoot", "chatwoot.42") is True
    assert await idempotency.claim(fake, "chatwoot", "chatwoot.42") is False  # held
    await idempotency.release(fake, "chatwoot", "chatwoot.42")
    # Released → the next claim wins again (retry re-processes).
    assert await idempotency.claim(fake, "chatwoot", "chatwoot.42") is True
    # Releasing an absent key is a no-op (best-effort contract).
    await idempotency.release(fake, "chatwoot", "does-not-exist")


@pytest.mark.asyncio
async def test_email_assist_enqueue_returns_false_when_full() -> None:
    """enqueue signals drop with False so the caller can recover."""
    from polaris_api.services import email_assist, email_assist_queue

    email_assist_queue.reset_queue_for_tests()
    job = email_assist.EmailAssistJob(
        tenant_id=uuid4(), channel_id=uuid4(),
        chatwoot_account_id=1, chatwoot_conversation_id=99,
        sender_email=None, body="hi",
    )
    q = email_assist_queue.get_queue()
    for _ in range(q.maxsize):  # fill to capacity
        q.put_nowait(job)
    assert await email_assist_queue.enqueue(job) is False  # dropped
    # Drain one slot → next enqueue is accepted.
    q.get_nowait()
    assert await email_assist_queue.enqueue(job) is True
    email_assist_queue.reset_queue_for_tests()
