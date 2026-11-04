"""Phase 2 PR-2 — webhook_resolver.resolve_meta + record_event.

DB-required integration tests. Skip when ``POLARIS_DATABASE_URL`` /
``SUPABASE_DB_URL`` aren't set (same pattern as Phase 1 tests).

These exercise the real ``services/webhook_resolver.py`` against migrated
``tenant_channels`` + ``webhook_events`` tables — proving:

  - Per-kind happy path (waba → whatsapp; ig → instagram; fb → messenger).
  - ADR-015 multiplicity (>1 active row matches → first by id wins,
    deterministically).
  - Unknown id → ``None``.
  - ``record_event`` writes a real row; subsequent insert with the same
    ``(source, external_id)`` is a no-op + returns the existing row id.
  - Tenant_id NULL rows (hmac_fail / tenant_not_found) round-trip cleanly.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from polaris_api.config import get_settings
from polaris_api.db import pools as pool_module
from polaris_api.services import secrets, webhook_resolver

_TEST_TENANT_PREFIX = "pytest-resolver-"


def _dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest_asyncio.fixture
async def pools_initialised() -> AsyncIterator[None]:
    """Open the real service pool (BYPASSRLS) against the test DSN.

    The resolver imports ``get_service_pool``; we point both pool DSNs
    at the same test connection string. The anon pool isn't used by
    PR-2's resolver but ``init_pools`` opens both — set both to the
    same DSN for simplicity.
    """
    dsn = _dsn()
    if not dsn:
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL to run resolver tests")

    # Snapshot prior pool state. Phase 1 tests may have left the
    # singletons primed; close them first so we re-init clean.
    await pool_module.close_pools()

    get_settings.cache_clear()
    settings = get_settings()
    settings.polaris_db_url_anon = dsn
    settings.polaris_db_url_service = dsn

    await pool_module.init_pools()
    try:
        yield
    finally:
        await pool_module.close_pools()
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def seed_conn(pools_initialised: None) -> AsyncIterator[asyncpg.Connection]:
    """A raw connection for seeding + cleanup. Distinct from the pool
    so its transactions don't interfere with the resolver's own
    connection acquisition."""
    _ = pools_initialised
    dsn = _dsn()
    assert dsn is not None
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        # Cleanup: rows seeded under the pytest tenant prefix.
        try:
            await conn.execute(
                f"""
                DELETE FROM webhook_events
                 WHERE tenant_id IN (
                   SELECT id FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'
                 )
                """  # noqa: S608
            )
            await conn.execute(
                f"""
                DELETE FROM webhook_events
                 WHERE tenant_id IS NULL
                   AND external_id LIKE '{_TEST_TENANT_PREFIX}%'
                """  # noqa: S608
            )
            await conn.execute(
                f"""
                DELETE FROM tenant_channels
                 WHERE tenant_id IN (
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


async def _seed_tenant_with_channel(
    conn: asyncpg.Connection,
    slug_suffix: str,
    *,
    waba_id: str | None = None,
    ig_id: str | None = None,
    ig_login_id: str | None = None,
    fb_id: str | None = None,
    telegram_secret: str | None = None,
    chatwoot_inbox_id: int,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert tenant + tenant_channels row; return (tenant_id, channel_id)."""
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id",
        f"{_TEST_TENANT_PREFIX}{slug_suffix}",
    )
    if telegram_secret is not None:
        channel = "telegram"
        kind_cols = "telegram_webhook_secret, telegram_bot_id"
        kind_vals = "$3, $4"
        params = (tenant_id, chatwoot_inbox_id, telegram_secret, f"{telegram_secret}-bot")
    elif waba_id is not None:
        channel = "whatsapp"
        kind_cols = "waba_id, phone_number_id"
        kind_vals = "$3, $4"
        params: tuple[object, ...] = (
            tenant_id,
            chatwoot_inbox_id,
            waba_id,
            f"{waba_id}-phone",
        )
    elif ig_id is not None:
        channel = "instagram_login"
        kind_cols = "ig_business_account_id"
        kind_vals = "$3"
        params = (tenant_id, chatwoot_inbox_id, ig_id)
    elif ig_login_id is not None:
        # instagram_login shares the ig_business_account_id column with the
        # legacy 'instagram' channel; only the channel name differs.
        channel = "instagram_login"
        kind_cols = "ig_business_account_id"
        kind_vals = "$3"
        params = (tenant_id, chatwoot_inbox_id, ig_login_id)
    elif fb_id is not None:
        channel = "messenger"
        kind_cols = "fb_page_id"
        kind_vals = "$3"
        params = (tenant_id, chatwoot_inbox_id, fb_id)
    else:
        raise ValueError("must specify one of waba_id / ig_id / fb_id")

    channel_id = await conn.fetchval(
        f"""
        INSERT INTO tenant_channels
            (tenant_id, channel, chatwoot_inbox_id, {kind_cols}, status)
        VALUES
            ($1, '{channel}', $2, {kind_vals}, 'active')
        RETURNING id
        """,  # noqa: S608
        *params,
    )
    return tenant_id, channel_id


@pytest.mark.asyncio
async def test_resolve_meta_waba_happy_path(seed_conn: asyncpg.Connection) -> None:
    waba = f"resolver-waba-{uuid.uuid4().hex[:8]}"
    tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "waba", waba_id=waba, chatwoot_inbox_id=1001
    )

    resolved = await webhook_resolver.resolve_meta(waba, "waba")
    assert resolved is not None
    assert resolved.tenant_id == tenant_id
    assert resolved.channel_id == channel_id
    assert resolved.channel_kind == "whatsapp"
    assert resolved.chatwoot_inbox_id == 1001


@pytest.mark.asyncio
async def test_resolve_meta_ig_happy_path(seed_conn: asyncpg.Connection) -> None:
    ig = f"resolver-ig-{uuid.uuid4().hex[:8]}"
    _tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "ig", ig_id=ig, chatwoot_inbox_id=1002
    )

    resolved = await webhook_resolver.resolve_meta(ig, "ig")
    assert resolved is not None
    assert resolved.channel_id == channel_id
    assert resolved.channel_kind == "instagram_login"


@pytest.mark.asyncio
async def test_resolve_meta_ig_login_returns_instagram_login(
    seed_conn: asyncpg.Connection,
) -> None:
    """An instagram_login row resolves via the same ig_business_account_id
    lookup but returns channel_kind='instagram_login' (read from the row's own
    channel column), so the dispatcher can pick the graph.instagram.com path."""
    ig = f"resolver-iglogin-{uuid.uuid4().hex[:8]}"
    _tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "iglogin", ig_login_id=ig, chatwoot_inbox_id=1004
    )

    resolved = await webhook_resolver.resolve_meta(ig, "ig")
    assert resolved is not None
    assert resolved.channel_id == channel_id
    assert resolved.channel_kind == "instagram_login"


@pytest.mark.asyncio
async def test_resolve_meta_fb_happy_path(seed_conn: asyncpg.Connection) -> None:
    fb = f"resolver-fb-{uuid.uuid4().hex[:8]}"
    _tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "fb", fb_id=fb, chatwoot_inbox_id=1003
    )

    resolved = await webhook_resolver.resolve_meta(fb, "fb")
    assert resolved is not None
    assert resolved.channel_id == channel_id
    assert resolved.channel_kind == "messenger"


@pytest.mark.asyncio
async def test_resolve_meta_unknown_returns_none(seed_conn: asyncpg.Connection) -> None:
    _ = seed_conn  # ensure pools are up
    resolved = await webhook_resolver.resolve_meta(
        "definitely-not-a-real-waba", "waba"
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_meta_inactive_channel_invisible(
    seed_conn: asyncpg.Connection,
) -> None:
    """Channels with status != 'active' must not resolve. Onboarding-revoked
    or quota-paused channels stay out of dispatch."""
    waba = f"resolver-waba-inactive-{uuid.uuid4().hex[:8]}"
    tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "waba-inactive", waba_id=waba, chatwoot_inbox_id=1004
    )
    _ = tenant_id
    await seed_conn.execute(
        "UPDATE tenant_channels SET status = 'revoked' WHERE id = $1",
        channel_id,
    )

    resolved = await webhook_resolver.resolve_meta(waba, "waba")
    assert resolved is None


# Telegram now resolves via resolve_chatwoot (native Channel::Telegram inbox) —
# the legacy own-webhook resolve_telegram was deleted. See
# test_resolve_chatwoot_telegram_happy_path below.


@pytest.mark.asyncio
async def test_record_event_inserts_unresolved_row(
    seed_conn: asyncpg.Connection,
) -> None:
    external_id = f"{_TEST_TENANT_PREFIX}hmac-fail-{uuid.uuid4().hex[:8]}"
    payload_hash = hashlib.sha256(b"unauthorised body bytes").digest()

    new_id = await webhook_resolver.record_event(
        source="meta",
        external_id=external_id,
        status="hmac_fail",
        payload_hash=payload_hash,
        error_class="HMACVerifyFailed",
    )

    row = await seed_conn.fetchrow(
        "SELECT tenant_id, status, error_class, payload_hash"
        " FROM webhook_events WHERE id = $1",
        new_id,
    )
    assert row is not None
    assert row["tenant_id"] is None
    assert row["status"] == "hmac_fail"
    assert row["error_class"] == "HMACVerifyFailed"
    assert bytes(row["payload_hash"]) == payload_hash


@pytest.mark.asyncio
async def test_record_event_dedupes_on_conflict(
    seed_conn: asyncpg.Connection,
) -> None:
    external_id = f"{_TEST_TENANT_PREFIX}dedup-{uuid.uuid4().hex[:8]}"
    payload_hash = hashlib.sha256(b"some body").digest()

    first_id = await webhook_resolver.record_event(
        source="meta",
        external_id=external_id,
        status="hmac_fail",
        payload_hash=payload_hash,
    )
    second_id = await webhook_resolver.record_event(
        source="meta",
        external_id=external_id,
        status="hmac_fail",
        payload_hash=payload_hash,
    )

    # ON CONFLICT DO NOTHING + UNION ALL fallback: the second call returns
    # the original row's id, NOT a new one. Naturally idempotent.
    assert first_id == second_id
    row_count = await seed_conn.fetchval(
        "SELECT COUNT(*) FROM webhook_events WHERE external_id = $1",
        external_id,
    )
    assert row_count == 1


@pytest.mark.asyncio
async def test_record_event_resolved_row(
    seed_conn: asyncpg.Connection,
) -> None:
    waba = f"resolver-record-{uuid.uuid4().hex[:8]}"
    tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "record", waba_id=waba, chatwoot_inbox_id=1005
    )
    external_id = f"{_TEST_TENANT_PREFIX}received-{uuid.uuid4().hex[:8]}"
    payload_hash = hashlib.sha256(b"valid body").digest()

    new_id = await webhook_resolver.record_event(
        source="meta",
        external_id=external_id,
        status="received",
        payload_hash=payload_hash,
        tenant_id=tenant_id,
        channel_id=channel_id,
    )
    row = await seed_conn.fetchrow(
        "SELECT tenant_id, channel_id, status FROM webhook_events WHERE id = $1",
        new_id,
    )
    assert row is not None
    assert row["tenant_id"] == tenant_id
    assert row["channel_id"] == channel_id
    assert row["status"] == "received"


@pytest.mark.asyncio
async def test_record_event_rejects_wrong_hash_length(
    pools_initialised: None,
) -> None:
    _ = pools_initialised
    with pytest.raises(ValueError, match="32 bytes"):
        await webhook_resolver.record_event(
            source="meta",
            external_id="hash-len-test",
            status="hmac_fail",
            payload_hash=b"too-short",
        )


async def _seed_email_channel(
    conn: asyncpg.Connection,
    slug_suffix: str,
    *,
    chatwoot_inbox_id: int,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert tenant + an email tenant_channels row.

    Email has NO channel-specific columns — Chatwoot owns the IMAP/SMTP/OAuth
    credentials — so this can't reuse ``_seed_tenant_with_channel`` (which
    requires a kind column). Returns (tenant_id, channel_id).
    """
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id",
        f"{_TEST_TENANT_PREFIX}{slug_suffix}",
    )
    channel_id = await conn.fetchval(
        """
        INSERT INTO tenant_channels (tenant_id, channel, chatwoot_inbox_id, status)
        VALUES ($1, 'email', $2, 'active')
        RETURNING id
        """,
        tenant_id,
        chatwoot_inbox_id,
    )
    return tenant_id, channel_id


@pytest.mark.asyncio
async def test_resolve_chatwoot_email_happy_path(seed_conn: asyncpg.Connection) -> None:
    """An email inbox resolves to channel_kind='email' (email-channel M1).

    resolve_chatwoot() must return the row for a Chatwoot-native email inbox
    so the email-assist consumer can fire — i.e. 'email' must be admitted by
    the migration 097 CHECK *and* the resolver's defensive guard set.
    """
    tenant_id, channel_id = await _seed_email_channel(
        seed_conn, "email", chatwoot_inbox_id=1201
    )

    resolved = await webhook_resolver.resolve_chatwoot(1201)
    assert resolved is not None
    assert resolved.tenant_id == tenant_id
    assert resolved.channel_id == channel_id
    assert resolved.channel_kind == "email"
    assert resolved.chatwoot_inbox_id == 1201


@pytest.mark.asyncio
async def test_resolve_chatwoot_telegram_happy_path(
    seed_conn: asyncpg.Connection,
) -> None:
    """A native Telegram inbox resolves to channel_kind='telegram'.

    Telegram is provisioned as a native Chatwoot ``Channel::Telegram`` inbox, so
    its inbound arrives through the agent-bot path and must resolve here (like the
    Meta surfaces). This asserts the resolver's guard admits 'telegram' so the
    bot-reply mirror + handoff get their Chatwoot conversation/account ids. (The
    legacy own-webhook ``resolve_telegram`` was deleted.)
    """
    tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "tg-chatwoot",
        telegram_secret=f"tg-cw-{uuid.uuid4().hex}", chatwoot_inbox_id=1205,
    )

    resolved = await webhook_resolver.resolve_chatwoot(1205)
    assert resolved is not None
    assert resolved.tenant_id == tenant_id
    assert resolved.channel_id == channel_id
    assert resolved.channel_kind == "telegram"
    assert resolved.chatwoot_inbox_id == 1205


@pytest.mark.asyncio
async def test_resolve_chatwoot_email_inactive_invisible(
    seed_conn: asyncpg.Connection,
) -> None:
    """A revoked/paused email inbox must not resolve (mirrors the Meta path)."""
    _tenant_id, channel_id = await _seed_email_channel(
        seed_conn, "email-inactive", chatwoot_inbox_id=1202
    )
    await seed_conn.execute(
        "UPDATE tenant_channels SET status = 'revoked' WHERE id = $1",
        channel_id,
    )

    resolved = await webhook_resolver.resolve_chatwoot(1202)
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_chatwoot_with_status_happy_path(
    seed_conn: asyncpg.Connection,
) -> None:
    """The combined one-round-trip resolver returns the channel row AND the
    owning tenant's status — behaviourally equal to resolve_chatwoot +
    fetch_tenant_status, used on the agent-bot hot path to save a round-trip."""
    tenant_id, channel_id = await _seed_tenant_with_channel(
        seed_conn, "cw-combined",
        telegram_secret=f"tg-cw-{uuid.uuid4().hex}", chatwoot_inbox_id=1210,
    )

    resolved, status_str, suspended_reason = (
        await webhook_resolver.resolve_chatwoot_with_status(1210)
    )
    assert resolved is not None
    assert resolved.tenant_id == tenant_id
    assert resolved.channel_id == channel_id
    assert resolved.channel_kind == "telegram"
    assert resolved.chatwoot_inbox_id == 1210
    assert status_str == "active"
    assert suspended_reason is None


@pytest.mark.asyncio
async def test_resolve_chatwoot_with_status_unknown_returns_none(
    seed_conn: asyncpg.Connection,
) -> None:
    """No active channel for the inbox → (None, None, None), like resolve_chatwoot."""
    _ = seed_conn  # ensure pools are up
    resolved, status_str, suspended_reason = (
        await webhook_resolver.resolve_chatwoot_with_status(987654321)
    )
    assert resolved is None
    assert status_str is None
    assert suspended_reason is None


# ---------- resolve_agent_forward_context WABA pinning (sub-fix A) ----------


async def _seed_two_waba_channels_and_conv(
    conn: asyncpg.Connection,
    slug_suffix: str,
    *,
    chatwoot_conversation_id: int,
    pin_to_new: bool,
) -> tuple[uuid.UUID, str, str]:
    """Seed a tenant with TWO whatsapp channels (old + new) + a conversation.

    Returns (tenant_id, old_phone_number_id, new_phone_number_id). The
    conversation's ``channel_id`` is set to the NEW channel when ``pin_to_new``,
    else left NULL (legacy row). A KMS-encrypted ``meta_waba_access_token`` is
    seeded so the resolver returns a context rather than None.
    """
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id",
        f"{_TEST_TENANT_PREFIX}{slug_suffix}",
    )
    old_pid = f"old-pid-{uuid.uuid4().hex[:8]}"
    new_pid = f"new-pid-{uuid.uuid4().hex[:8]}"
    # Old channel: created a day earlier so created_at ASC picks it on fallback.
    await conn.execute(
        """
        INSERT INTO tenant_channels
            (tenant_id, channel, chatwoot_inbox_id, waba_id, phone_number_id,
             status, created_at)
        VALUES ($1, 'whatsapp', $2, $3, $4, 'active', now() - interval '1 day')
        """,
        tenant_id, 5001, f"waba-old-{uuid.uuid4().hex[:8]}", old_pid,
    )
    new_channel_id = await conn.fetchval(
        """
        INSERT INTO tenant_channels
            (tenant_id, channel, chatwoot_inbox_id, waba_id, phone_number_id,
             status, created_at)
        VALUES ($1, 'whatsapp', $2, $3, $4, 'active', now())
        RETURNING id
        """,
        tenant_id, 5002, f"waba-new-{uuid.uuid4().hex[:8]}", new_pid,
    )
    await conn.execute(
        """
        INSERT INTO conversations
            (tenant_id, channel, channel_user_id, channel_id,
             chatwoot_conversation_id)
        VALUES ($1, 'whatsapp', $2, $3, $4)
        """,
        tenant_id,
        f"6012{uuid.uuid4().hex[:6]}",
        new_channel_id if pin_to_new else None,
        chatwoot_conversation_id,
    )
    await secrets.put(conn, tenant_id, "meta_waba_access_token", b"waba-token-xyz")
    return tenant_id, old_pid, new_pid


@pytest.mark.asyncio
async def test_agent_forward_pins_to_conversation_channel(
    seed_conn: asyncpg.Connection,
) -> None:
    """A multi-WABA tenant forwards the agent reply from the SAME number the
    customer messaged — the conversation's pinned channel_id — not the oldest."""
    if not os.environ.get("POLARIS_KMS_KEY"):
        pytest.skip("set POLARIS_KMS_KEY to run agent-forward WABA tests")
    tenant_id, old_pid, new_pid = await _seed_two_waba_channels_and_conv(
        seed_conn, "waba-pin", chatwoot_conversation_id=7001, pin_to_new=True
    )
    ctx = await webhook_resolver.resolve_agent_forward_context(tenant_id, 7001)
    assert ctx is not None
    assert ctx.channel == "whatsapp"
    assert ctx.phone_number_id == new_pid  # pinned, NOT the older row
    assert ctx.phone_number_id != old_pid


@pytest.mark.asyncio
async def test_agent_forward_legacy_null_channel_falls_back_to_oldest(
    seed_conn: asyncpg.Connection,
) -> None:
    """A conversation created before migration 106 (channel_id NULL) falls back
    to the oldest active WABA row — deterministic, correct for single-WABA."""
    if not os.environ.get("POLARIS_KMS_KEY"):
        pytest.skip("set POLARIS_KMS_KEY to run agent-forward WABA tests")
    tenant_id, old_pid, _new_pid = await _seed_two_waba_channels_and_conv(
        seed_conn, "waba-legacy", chatwoot_conversation_id=7002, pin_to_new=False
    )
    ctx = await webhook_resolver.resolve_agent_forward_context(tenant_id, 7002)
    assert ctx is not None
    assert ctx.phone_number_id == old_pid  # oldest, via created_at ASC fallback
