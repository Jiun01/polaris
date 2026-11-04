"""E2E sweep — off-Meta channels (Telegram M1/M2/M5 + website widget M3/M4).

Real DB (skips without POLARIS_DB_URL_ANON + POLARIS_DB_URL_SERVICE). Drives
EVERY off-Meta surface in one flow against the live Postgres so migrations
094 + 095, RLS, the chat-host allowlist, and KMS encryption are all exercised
for real. Only the two genuinely-external dependencies are stubbed:

  * the Telegram Bot API HTTP egress (getMe / setWebhook / sendMessage) — a
    recording shim swapped into ``telegram_client.httpx`` so the REAL client
    code (URL build, _request, error handling) runs; only the socket is faked;
  * the LLM (mock_anthropic + mock_cohere) + zero-vector embed — deterministic.

Surfaces proven, in order:
  M2  POST /api/admin/tenants/{id}/channels/telegram → getMe ONLY (Chatwoot-native
      cutover: NO setWebhook — Chatwoot owns the bot webhook; telegram_webhook_secret
      stays NULL) + real provision row + KMS-encrypted tenant_secrets token (tier-gated).
  M1  native Telegram outbound: pipeline.run on a provider='telegram'
      IncomingMessage → messages rows → real KMS token decrypt → sendMessage
      egress. The legacy own-webhook router (resolve_telegram) was DELETED;
      native inbound enqueue (Chatwoot agent-bot path) is covered by
      test_chatwoot_router::test_native_telegram_inbound_enqueues_with_cw_ids.
  M3/4 PUT/GET /api/tenant/widget (real upsert + RLS) → GET public config
      returns each channel's direct https target (read-only; no redirect/writes).
  M5  human-agent reply over a telegram conversation → NO Polaris egress
      (cutover: Chatwoot forwards human-agent replies to the customer natively;
      Polaris re-sending would double-send, so the forward path was deleted).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import asyncpg
import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from polaris_api.config import get_settings
from polaris_api.db import pools as pool_module
from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.main import app
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services import embed as embed_module
from polaris_api.services import inbound_queue
from polaris_api.services import pipeline as pipeline_module
from polaris_api.services import telegram_client as tg_client_module
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.webhooks import chatwoot_router as chatwoot_router_module
from polaris_shared_types import IncomingMessage, SenderRef

_PREFIX = "pytest-e2e-offmeta-"
_ADMIN_ID = uuid.uuid4()

# This file builds its own AsyncClient against the main app AND overrides async
# deps via app.dependency_overrides + opens/closes the global pools. Per conftest
# (see the "function-scoped admin test fixtures + session event loop" note), such
# files MUST pin loop_scope="function" on the marker and every async fixture, or
# function-scope teardown bleeds the event loop / pool state into the next test
# file alphabetically after it (surfacing there as spurious pool/visibility
# errors). test_admin_force_run.py is the reference pattern.
pytestmark = pytest.mark.asyncio(loop_scope="function")


def _dsn() -> str | None:
    s = get_settings()
    return s.polaris_db_url_service or s.polaris_db_url_anon or None


# ---------------------------------------------------------------------------
# Telegram HTTP egress recorder — real client code, faked socket.
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.calls.clear()

    def by_method(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method]


def _install_telegram_recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    import httpx as _httpx

    class _StubAsyncClient:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        async def __aenter__(self) -> _StubAsyncClient:
            return self

        async def __aexit__(self, *_e: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any] | None = None) -> _httpx.Response:
            method = url.rsplit("/", 1)[1]
            rec.calls.append({"method": method, "url": url, "json": json})
            if method == "getMe":
                body = {"ok": True, "result": {"id": 7777777, "username": "e2e_acme_bot"}}
            elif method == "setWebhook":
                body = {"ok": True, "result": True}
            elif method in ("sendMessage", "sendPhoto"):
                body = {"ok": True, "result": {"message_id": 9100 + len(rec.calls)}}
            else:
                body = {"ok": True, "result": True}
            return _httpx.Response(200, json=body)

    class _HttpxShim:
        AsyncClient = _StubAsyncClient
        Response = _httpx.Response
        RequestError = _httpx.RequestError

    monkeypatch.setattr(tg_client_module, "httpx", _HttpxShim)
    return rec


# ---------------------------------------------------------------------------
# Fixtures — real pools, fakeredis, LLM/embed stubs, auth overrides.
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_settings() -> Iterator[None]:
    # secrets.py reads POLARIS_KMS_KEY from os.environ directly (not via
    # pydantic-settings), so populate os.environ from .env for the real
    # XChaCha20 encrypt/decrypt round-trip. Does not override existing vars.
    #
    # A bare load_dotenv() LEAKS every .env key (incl. SUPABASE_DB_URL) into
    # os.environ for the rest of the session, with no cleanup. That flips the
    # DB-gated RLS tests in other files from "skip" to "attempt a real
    # connection", producing cascading collection errors in the full suite.
    # So mirror load_dotenv(override=False) — only add keys not already set —
    # but track what we added and remove it again in teardown.
    from dotenv import dotenv_values, find_dotenv

    added: list[str] = []
    for key, value in dotenv_values(find_dotenv(usecwd=True)).items():
        if value is not None and key not in os.environ:
            os.environ[key] = value
            added.append(key)
    get_settings.cache_clear()
    settings = get_settings()
    settings.public_api_base_url = "https://api.e2e.test"
    try:
        yield
    finally:
        for key in added:
            os.environ.pop(key, None)
        get_settings.cache_clear()


@pytest_asyncio.fixture(loop_scope="function")
async def pools_open(configured_settings: None) -> AsyncIterator[None]:
    _ = configured_settings
    if not _dsn():
        pytest.skip("set POLARIS_DB_URL_ANON + POLARIS_DB_URL_SERVICE to run E2E")
    if (
        os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"
    ) and not os.environ.get("POLARIS_DB_URL_ANON"):
        pytest.skip("CI: anon/service roles not bootstrapped")
    await pool_module.close_pools()
    await pool_module.init_pools()
    try:
        yield
    finally:
        await pool_module.close_pools()


@pytest_asyncio.fixture(loop_scope="function")
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
                f"DELETE FROM webhook_events WHERE tenant_id IN (SELECT id FROM tenants WHERE slug LIKE '{_PREFIX}%')"  # noqa: S608, E501
            )
            # Use the hard-delete fn, not a bare DELETE FROM tenants: the connect
            # flow writes an append-only audit_logs row (no ON DELETE CASCADE),
            # so a plain delete FK-fails + (silently) leaks the tenant into prod
            # Supabase. admin_hard_delete_tenant() clears audit_logs + the other
            # non-cascading children first, then the tenant (cascading the rest).
            leaked = await conn.fetch(
                f"SELECT id FROM tenants WHERE slug LIKE '{_PREFIX}%'"  # noqa: S608
            )
            for r in leaked:
                await conn.execute("SELECT admin_hard_delete_tenant($1)", r["id"])
        except Exception:  # noqa: BLE001, S110
            pass
        await conn.close()


async def _seed_tenant(conn: asyncpg.Connection, *, slug_suffix: str) -> uuid.UUID:
    slug = f"{_PREFIX}{slug_suffix}"
    tenant_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug
    )
    # Quotas with telegram allowed (Pro+ gate is membership in allowed_channel_kinds).
    await conn.execute(
        """
        INSERT INTO tenant_quotas (
            tenant_id, bot_replies, conversation_retention_days,
            allowed_channel_kinds, outbound_broadcast_enabled,
            multi_branch_enabled, knowledge_chunks_total_mb,
            rate_limit_per_minute, monthly_llm_tokens_in,
            monthly_llm_tokens_out, monthly_ingestion_pages,
            monthly_broadcast_recipients, monthly_bookings,
            bot_replies_period_cap, plan_tier
        ) VALUES ($1, 10000, 90, ARRAY['whatsapp','telegram'], false, false,
                  100, 60, 1000000, 1000000, 1000, 10000, 1000, 10000, 'pro')
        """,
        tenant_id,
    )
    return tenant_id


async def _seed_chunk(conn: asyncpg.Connection, tenant_id: uuid.UUID, text: str) -> None:
    import hashlib

    request_id = await conn.fetchval(
        "INSERT INTO ingestion_requests (tenant_id, source_type, source_text, status) "
        "VALUES ($1, 'text', $2, 'completed') RETURNING id",
        tenant_id, text,
    )
    zero = "[" + ",".join(["0"] * 1024) + "]"
    await conn.execute(
        """
        INSERT INTO knowledge_chunks
            (tenant_id, request_id, source_name, source_type, chunk_index,
             content, content_hash, embedding, token_count)
        VALUES ($1, $2, 'faq.md', 'text', 0, $3, $4, $5::vector, $6)
        """,
        tenant_id, request_id, text,
        hashlib.sha256(text.encode()).hexdigest(), zero, max(1, len(text) // 4),
    )


@pytest_asyncio.fixture(loop_scope="function")
async def wired(
    pools_open: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, fakeredis.aioredis.FakeRedis, _Recorder]]:
    _ = pools_open
    # Fake redis is passed straight into PipelineDeps for the M1 native run. It
    # ALSO needs wiring into get_redis(): since #108, the human-agent-reply path
    # (_handle_human_agent_reply) calls get_redis() directly rather than taking it
    # from deps, so without this the off-meta human-agent assertion 500s.
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    from polaris_api.webhooks import chatwoot_router as _cwr_mod
    monkeypatch.setattr(_cwr_mod, "get_redis", lambda: fake)

    embed_module._reset_client_for_testing()

    async def _zero_vec(_t: str, **_kw: Any) -> list[float]:
        # pipeline.run now calls embed_query(text, tenant_id=...) — accept and
        # ignore the keyword so the stub matches the live call signature.
        return [0.0] * 1024

    monkeypatch.setattr(embed_module, "embed_query", _zero_vec)

    rec = _install_telegram_recorder(monkeypatch)
    inbound_queue.reset_queue_for_tests()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, fake, rec
    finally:
        await fake.aclose()
        inbound_queue.reset_queue_for_tests()


# ---------------------------------------------------------------------------
# The sweep.
# ---------------------------------------------------------------------------


async def test_off_meta_channels_all_surfaces(
    wired: tuple[AsyncClient, fakeredis.aioredis.FakeRedis, _Recorder],
    seed_conn: asyncpg.Connection,
    mock_anthropic: MagicMock,
    mock_cohere: MagicMock,
) -> None:
    from tests.conftest import make_mock_anthropic_message

    client, fake, rec = wired
    _ = mock_cohere
    suffix = uuid.uuid4().hex[:8]
    tenant_id = await _seed_tenant(seed_conn, slug_suffix=suffix)
    await _seed_chunk(seed_conn, tenant_id, "we open monday to friday 9am to 6pm")
    inbox_id = abs(hash(suffix)) % 1_000_000_000

    # ---- auth overrides: real connections, no JWT ----------------------
    async def _admin_conn() -> AsyncIterator[asyncpg.Connection]:
        pool = pool_module.get_service_pool()
        async with pool.acquire() as conn:
            yield conn

    async def _req_conn() -> AsyncIterator[asyncpg.Connection]:
        pool = pool_module.get_anon_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_id)
            )
            yield conn

    app.dependency_overrides[get_admin_service_conn] = _admin_conn
    app.dependency_overrides[require_platform_admin] = lambda: _ADMIN_ID
    app.dependency_overrides[get_request_conn] = _req_conn
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=tenant_id, user_id=uuid.uuid4(), role="owner",
        plan_tier="pro", status="active",
    )

    try:
        # ============ M2: admin connect Telegram ============
        resp = await client.post(
            f"/api/admin/tenants/{tenant_id}/channels/telegram",
            json={"bot_token": "777:E2E-TOKEN", "chatwoot_inbox_id": inbox_id},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["bot_username"] == "e2e_acme_bot"
        # Chatwoot-native cutover: real Telegram egress is getMe ONLY — Polaris
        # must NOT setWebhook (Chatwoot owns the bot webhook now).
        assert len(rec.by_method("getMe")) == 1
        assert rec.by_method("setWebhook") == [], rec.calls

        # Real DB row + KMS-encrypted token landed; NO webhook secret persisted.
        ch = await seed_conn.fetchrow(
            "SELECT channel, telegram_bot_id, telegram_bot_username, "
            "telegram_webhook_secret, chatwoot_inbox_id FROM tenant_channels "
            "WHERE tenant_id = $1 AND channel = 'telegram'",
            tenant_id,
        )
        assert ch["telegram_bot_id"] == "7777777"
        assert ch["telegram_bot_username"] == "e2e_acme_bot"
        assert ch["telegram_webhook_secret"] is None  # Chatwoot owns the webhook

        from polaris_api.services import secrets as secrets_service

        tok = await secrets_service.get(seed_conn, tenant_id, "telegram_bot_token")
        assert tok == b"777:E2E-TOKEN"  # round-trips through real XChaCha20 + KMS

        # ============ M1: native Telegram inbound → pipeline → outbound ============
        # Telegram is a native Chatwoot channel now — the legacy own-webhook
        # router + resolve_telegram were DELETED. Native inbound enqueue (Chatwoot
        # agent-bot → resolve_chatwoot → IncomingMessage with CW ids) is covered by
        # test_chatwoot_router::test_native_telegram_inbound_enqueues_with_cw_ids.
        # Here we drive pipeline.run directly with the IncomingMessage that path
        # produces to prove the REAL outbound: KMS-decrypt the bot token from
        # tenant_secrets → real Telegram sendMessage egress. No chatwoot_* ids are
        # set, so the best-effort CW reply-mirror is skipped (no Chatwoot HTTP
        # call) — this isolates the token-decrypt + Bot-API egress path.
        tg_channel_id = await seed_conn.fetchval(
            "SELECT id FROM tenant_channels WHERE tenant_id = $1 AND channel = 'telegram'",
            tenant_id,
        )
        rec.reset()
        chat_id = 55_500_001
        mock_anthropic.messages.create.side_effect = [
            make_mock_anthropic_message(text="simple"),
            make_mock_anthropic_message(
                text="We open Mon-Fri 9am-6pm.", input_tokens=120, output_tokens=9
            ),
        ]
        incoming = IncomingMessage(
            tenant_id=tenant_id,
            channel_id=tg_channel_id,
            provider="telegram",
            provider_message_id="chatwoot.500",  # native: dedup id is Chatwoot's
            sender=SenderRef(channel_user_id=str(chat_id), display_name="Jane"),
            body="what time you open",
            received_at=datetime.now(UTC),
        )
        deps = pipeline_module.PipelineDeps(
            anon_pool=pool_module.get_anon_pool(), redis=fake, meta_client=MagicMock(),
        )
        decision = await pipeline_module.run(incoming=incoming, deps=deps)
        assert decision.tenant_id == tenant_id

        # messages persisted (real DB).
        msgs = await seed_conn.fetch(
            "SELECT role, content FROM messages WHERE tenant_id = $1", tenant_id
        )
        by_role = {m["role"]: m["content"] for m in msgs}
        assert by_role.get("user") == "what time you open"
        assert "assistant" in by_role

        # The bot reply egressed to Telegram via the REAL token (decrypted from
        # tenant_secrets) — sendMessage to the customer's chat_id.
        sends = rec.by_method("sendMessage")
        assert len(sends) == 1, rec.calls
        assert sends[0]["json"]["chat_id"] == str(chat_id)
        assert sends[0]["json"]["text"] == "We open Mon-Fri 9am-6pm."

        # ============ M3/M4: website widget ============
        rec.reset()
        put = await client.put(
            "/api/tenant/widget",
            json={
                "enabled": True,
                "greeting": "Talk to us",
                "channels": [
                    {"kind": "telegram", "label": "Telegram", "target": "https://t.me/e2e_acme_bot"},
                    {"kind": "whatsapp", "label": "WhatsApp", "target": "https://wa.me/60123456789"},
                ],
            },
        )
        assert put.status_code == 200, put.text
        widget_key = put.json()["widget_key"]
        assert "/widget.js" in put.json()["embed_snippet"]

        # Reject a non-allowlisted target (open-redirect guard, real validation).
        bad = await client.put(
            "/api/tenant/widget",
            json={"enabled": True, "greeting": "x",
                  "channels": [{"kind": "x", "label": "x", "target": "https://evil.com/x"}]},
        )
        assert bad.status_code == 422

        # Public config (no auth) — read-only: returns each channel's DIRECT
        # https target so the bubble links straight to it (no server redirect).
        cfg = await client.get(f"/api/v1/widget/{widget_key}/config")
        assert cfg.status_code == 200
        chans = cfg.json()["channels"]
        assert [c["kind"] for c in chans] == ["telegram", "whatsapp"]
        assert chans[0]["target"] == "https://t.me/e2e_acme_bot"
        assert chans[1]["target"] == "https://wa.me/60123456789"
        assert cfg.headers["cache-control"] == "public, max-age=60"

        # /widget.js is served (the bubble that renders the direct-link anchors).
        js = await client.get("/widget.js")
        assert js.status_code == 200
        assert "data-polaris-widget" in js.text

        # ===== M5: cutover — Chatwoot forwards human-agent replies natively =====
        # Pre-cutover, Polaris re-sent a human agent's Chatwoot reply to the
        # customer channel (``_dispatch_agent_forward``). Once Chatwoot owns the
        # channels it forwards those replies itself, so Polaris re-sending was a
        # double-send (visible on Messenger/IG) — the forward path was deleted.
        # Assert the agent-reply path is now a NO-OP for a non-email (telegram)
        # inbox: a genuine human-agent outgoing message produces ZERO egress.
        rec.reset()
        cw_conv_id = 880_001
        from unittest.mock import AsyncMock as _AM

        from polaris_api.services.webhook_resolver import ResolvedChannel

        chatwoot_router_module.resolve_chatwoot = _AM(  # type: ignore[assignment]
            return_value=ResolvedChannel(
                tenant_id=tenant_id, channel_id=uuid.uuid4(),
                channel_kind="telegram", chatwoot_inbox_id=inbox_id,
            )
        )
        agent_payload = {
            "message_type": "outgoing",
            "sender": {"type": "agent", "id": 9},
            "inbox": {"id": inbox_id},
            "content": "An agent here — we open at 9.",
            "conversation": {"id": cw_conv_id},
        }
        # The non-incoming branch routes a human-agent reply here; for telegram
        # it resolves to a non-email channel and returns without sending.
        fwd_inbox_id = chatwoot_router_module.human_agent_forward_inbox_id(agent_payload)
        assert fwd_inbox_id == inbox_id
        await chatwoot_router_module._handle_human_agent_reply(fwd_inbox_id, agent_payload)
        # Chatwoot delivers the agent's telegram reply itself now — Polaris must NOT.
        assert rec.by_method("sendMessage") == [], rec.calls

    finally:
        app.dependency_overrides.clear()
