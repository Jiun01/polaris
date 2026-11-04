"""Phase 2 PR-5 — Chatwoot router end-to-end (DB-backed).

Mirrors ``test_meta_router_replay.py`` against the Chatwoot endpoint.
Asserts full pipeline: HMAC pass → resolve_chatwoot → status gate →
SETNX claim → IncomingMessage enqueue + ``received`` row.

DB + fakeredis required. Skips without ``POLARIS_DATABASE_URL`` /
``SUPABASE_DB_URL``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import asyncpg
import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from polaris_api.config import get_settings
from polaris_api.db import pools as pool_module
from polaris_api.main import app
from polaris_api.services import inbound_queue
from polaris_api.webhooks import chatwoot_router as chatwoot_router_module

_FAKE_SECRET = "phase-2-pr-5-chatwoot-db-secret"
_TEST_TENANT_PREFIX = "pytest-chatwoot-"


def _dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


def _sign(raw: bytes) -> str:
    return hmac.new(_FAKE_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


@pytest.fixture
def configured_settings() -> Iterator[None]:
    get_settings.cache_clear()
    settings = get_settings()
    settings.chatwoot_webhook_hmac_secret = _FAKE_SECRET
    dsn = _dsn() or ""
    settings.polaris_db_url_anon = dsn
    settings.polaris_db_url_service = dsn
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def pools_open(configured_settings: None) -> AsyncIterator[None]:
    _ = configured_settings
    if not _dsn():
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL to run chatwoot test")
    await pool_module.close_pools()
    await pool_module.init_pools()
    try:
        yield
    finally:
        await pool_module.close_pools()


@pytest_asyncio.fixture
async def seed_conn(pools_open: None) -> AsyncIterator[asyncpg.Connection]:
    _ = pools_open
    dsn = _dsn()
    assert dsn is not None
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        try:
            await conn.execute(
                f"""
                DELETE FROM webhook_events WHERE tenant_id IN (
                  SELECT id FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'
                )
                """  # noqa: S608
            )
            await conn.execute(
                """
                DELETE FROM webhook_events
                 WHERE tenant_id IS NULL AND external_id LIKE '%pytest-chatwoot%'
                """
            )
            await conn.execute(
                f"""
                DELETE FROM tenant_channels WHERE tenant_id IN (
                  SELECT id FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'
                )
                """  # noqa: S608
            )
            await conn.execute(
                f"DELETE FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'"  # noqa: S608
            )
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass
        await conn.close()


@pytest_asyncio.fixture
async def chatwoot_client(
    pools_open: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    _ = pools_open
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(chatwoot_router_module, "get_redis", lambda: fake)
    inbound_queue.reset_queue_for_tests()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await fake.aclose()
        inbound_queue.reset_queue_for_tests()


async def _seed_active_tenant_with_inbox(
    conn: asyncpg.Connection,
) -> tuple[uuid.UUID, int]:
    slug = f"{_TEST_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug
    )
    inbox_id = abs(hash(slug)) % 1_000_000_000
    waba = f"chatwoot-waba-{uuid.uuid4().hex[:8]}"
    await conn.execute(
        """
        INSERT INTO tenant_channels
            (tenant_id, channel, chatwoot_inbox_id, waba_id, phone_number_id, status)
        VALUES ($1, 'whatsapp', $2, $3, $4, 'active')
        """,
        tenant_id,
        inbox_id,
        waba,
        f"{waba}-phone",
    )
    return tenant_id, inbox_id


async def _seed_active_tenant_with_telegram_inbox(
    conn: asyncpg.Connection,
) -> tuple[uuid.UUID, int]:
    """Seed a native-Telegram tenant: ``channel='telegram'`` bound to a Chatwoot
    inbox. Chatwoot-native Telegram cutover — the bot is a Chatwoot
    ``Channel::Telegram`` inbox, so inbound rides the agent-bot path and must
    ``resolve_chatwoot`` to channel_kind='telegram' just like the Meta surfaces.
    """
    slug = f"{_TEST_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug
    )
    inbox_id = abs(hash(slug)) % 1_000_000_000
    secret = f"tg-cw-{uuid.uuid4().hex[:8]}"
    await conn.execute(
        """
        INSERT INTO tenant_channels
            (tenant_id, channel, chatwoot_inbox_id,
             telegram_webhook_secret, telegram_bot_id, status)
        VALUES ($1, 'telegram', $2, $3, $4, 'active')
        """,
        tenant_id,
        inbox_id,
        secret,
        f"{secret}-bot",
    )
    return tenant_id, inbox_id


def _telegram_payload(inbox_id: int, msg_id: int, chat_id: str) -> bytes:
    """A Chatwoot agent-bot ``message_created`` for a native-Telegram inbox.

    ``conversation.status='pending'`` clears the human-handling gate (bot owns
    it); ``contact_inbox.source_id`` carries the Telegram chat id the reply
    dispatches to; ``account.id`` + ``conversation.id`` are the CW ids the
    pipeline LINKS onto the conversation row so the bot-reply mirror + handoff
    target the real Chatwoot conversation.
    """
    return json.dumps(
        {
            "event": "message_created",
            "id": msg_id,
            "account": {"id": 4242},
            "inbox": {"id": inbox_id},
            "conversation": {
                "id": 880_777,
                "status": "pending",
                "contact_inbox": {"source_id": chat_id},
            },
            "sender": {"id": 7, "name": "Jane"},
            "content": "hi from telegram",
            "message_type": "incoming",
            "created_at": "2026-06-12T08:00:00Z",
        }
    ).encode("utf-8")


# Recipient (WA wa_id) carried on contact_inbox.source_id — the canonical source
# the post-rehaul router resolves the real reply recipient from.
_WA_RECIPIENT = "60123456789"


def _payload(inbox_id: int, msg_id: int, content: str = "from chatwoot") -> bytes:
    """A Chatwoot agent-bot ``message_created`` for a native WhatsApp inbox.

    Post-rehaul shape: ``conversation.status='pending'`` clears the
    human-handling gate (bot owns it — otherwise the router records
    ``human_handling`` and returns without enqueuing), and
    ``contact_inbox.source_id`` carries the WA wa_id the router resolves the
    real reply recipient from. ``account.id`` + ``conversation.id`` are the CW
    ids the pipeline LINKS onto the conversation row.
    """
    return json.dumps(
        {
            "event": "message_created",
            "id": msg_id,
            "account": {"id": 1},
            "inbox": {"id": inbox_id},
            "conversation": {
                "id": 99,
                "status": "pending",
                "contact_inbox": {"source_id": _WA_RECIPIENT},
            },
            "sender": {"id": 7, "name": "Jane"},
            "content": content,
            "message_type": "incoming",
            "created_at": "2026-05-17T08:00:00Z",
        }
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_active_tenant_received_and_enqueued(
    chatwoot_client: AsyncClient, seed_conn: asyncpg.Connection
) -> None:
    tenant_id, inbox_id = await _seed_active_tenant_with_inbox(seed_conn)
    msg_id = int(uuid.uuid4().int % 1_000_000_000)
    body = _payload(inbox_id, msg_id)

    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body),
        },
    )
    assert resp.status_code == 200

    statuses = [
        r["status"]
        for r in await seed_conn.fetch(
            "SELECT status FROM webhook_events WHERE tenant_id = $1", tenant_id
        )
    ]
    assert statuses == ["received"]

    queue = inbound_queue.get_queue()
    assert queue.qsize() == 1
    msg = await queue.get()
    # Post-rehaul: chatwoot is the front-door but the IncomingMessage is built on
    # the REAL channel (whatsapp), addressed to the REAL recipient resolved from
    # contact_inbox.source_id — NOT provider='chatwoot' (rejected by the pipeline)
    # nor the Chatwoot contact/sender id.
    assert msg.provider == "whatsapp"
    assert msg.provider_message_id == f"chatwoot.{msg_id}"
    assert msg.body == "from chatwoot"
    assert msg.sender.channel_user_id == _WA_RECIPIENT
    # CW ids ride through so pipeline._link_cw_conversation links the mirror.
    assert msg.chatwoot_conversation_id == 99
    assert msg.chatwoot_account_id == 1
    queue.task_done()


@pytest.mark.asyncio
async def test_full_inbound_queue_releases_idem_claim_and_503s(
    chatwoot_client: AsyncClient,
    seed_conn: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """silent-failure audit fix A6 (2026-07-02): when inbound_queue.enqueue
    reports the queue is full, the router must release the idempotency claim
    + 503 (so the provider retries) instead of 200-acking a message that was
    silently dropped."""
    tenant_id, inbox_id = await _seed_active_tenant_with_inbox(seed_conn)
    msg_id = int(uuid.uuid4().int % 1_000_000_000)
    body = _payload(inbox_id, msg_id)

    monkeypatch.setattr(chatwoot_router_module, "enqueue", AsyncMock(return_value=False))

    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body),
        },
    )
    assert resp.status_code == 503

    # No 'received' row was recorded — the idempotency claim was released so
    # a provider retry re-processes rather than being deduped as a replay.
    statuses = [
        r["status"]
        for r in await seed_conn.fetch(
            "SELECT status FROM webhook_events WHERE tenant_id = $1", tenant_id
        )
    ]
    assert statuses == []


@pytest.mark.skipif(
    os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true",
    reason=(
        "CI follow-up (2026-05-19): per-test function-scoped fakeredis + "
        "module-level monkeypatch of get_redis loses the patch by the time "
        "the second POST runs in CI's session-loop config; the second "
        "request sees a fresh-looking idempotency key + re-enqueues (qsize 2). "
        "Local runs pass. Tracked separately."
    ),
)
@pytest.mark.asyncio
async def test_redelivery_is_deduped_no_double_enqueue(
    chatwoot_client: AsyncClient, seed_conn: asyncpg.Connection
) -> None:
    """A re-delivered Chatwoot message must NOT be enqueued twice.

    The real idempotency guarantee is the single enqueue: the second POST's
    ``idem_claim`` returns False (key already set) so the message is not
    re-processed. ``webhook_events`` records the message ONCE as 'received' —
    note the replay attempt's ``record_event(status='replay_ignored')`` reuses
    the SAME ``external_id`` (``chatwoot.{id}``) as the received row, and the
    table's ``UNIQUE (source, external_id)`` + ``ON CONFLICT DO NOTHING`` means
    that replay row collides and is silently dropped. So we assert the
    enqueue-once invariant + the lone 'received' row, NOT a separate
    'replay_ignored' row (which cannot coexist with 'received' by construction).
    """
    tenant_id, inbox_id = await _seed_active_tenant_with_inbox(seed_conn)
    msg_id = int(uuid.uuid4().int % 1_000_000_000)
    body = _payload(inbox_id, msg_id)

    resp1 = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body),
        },
    )
    resp2 = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body),
        },
    )
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # Enqueued exactly once — the second delivery was deduped, not re-processed.
    assert inbound_queue.get_queue().qsize() == 1

    statuses = [
        r["status"]
        for r in await seed_conn.fetch(
            "SELECT status FROM webhook_events WHERE tenant_id = $1", tenant_id
        )
    ]
    # Single 'received' row: the replay_ignored insert collides on external_id
    # with the received row (ON CONFLICT DO NOTHING) and is dropped.
    assert statuses == ["received"]


@pytest.mark.asyncio
async def test_native_telegram_inbound_enqueues_with_cw_ids(
    chatwoot_client: AsyncClient, seed_conn: asyncpg.Connection
) -> None:
    """Native-Telegram inbound resolves + enqueues an IncomingMessage carrying
    the Chatwoot conversation/account ids — the mirror linkage the rehaul
    dropped for Telegram.

    Regression guard for the Chatwoot-native Telegram cutover: pre-fix,
    ``resolve_chatwoot`` rejected channel_kind='telegram' (Telegram rode its own
    legacy own-webhook router — since DELETED — which built an IncomingMessage
    with NO ``chatwoot_*`` ids → ``_link_cw_conversation`` no-op'd → no CW mirror
    / handoff / agent-forward target). With Telegram provisioned as a native
    Chatwoot inbox, inbound rides the agent-bot path and the enqueued message
    must carry provider='telegram', the chat-id recipient, AND both CW ids so the
    pipeline links the conversation.
    """
    tenant_id, inbox_id = await _seed_active_tenant_with_telegram_inbox(seed_conn)
    msg_id = int(uuid.uuid4().int % 1_000_000_000)
    chat_id = "777001"
    body = _telegram_payload(inbox_id, msg_id, chat_id)

    resp = await chatwoot_client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Signature": _sign(body),
        },
    )
    assert resp.status_code == 200

    statuses = [
        r["status"]
        for r in await seed_conn.fetch(
            "SELECT status FROM webhook_events WHERE tenant_id = $1", tenant_id
        )
    ]
    assert statuses == ["received"]

    queue = inbound_queue.get_queue()
    assert queue.qsize() == 1
    msg = await queue.get()
    assert msg.provider == "telegram"
    assert msg.provider_message_id == f"chatwoot.{msg_id}"
    # Reply must dispatch to the Telegram chat id (from contact_inbox.source_id),
    # NOT the Chatwoot contact id.
    assert msg.sender.channel_user_id == chat_id
    # The CW ids ride through so pipeline._link_cw_conversation links (never
    # re-creates) the Chatwoot mirror.
    assert msg.chatwoot_conversation_id == 880_777
    assert msg.chatwoot_account_id == 4242
    queue.task_done()
