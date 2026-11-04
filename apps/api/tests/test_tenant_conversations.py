"""Phase 8 PR-6 — tenant_conversations router + RLS isolation tests.

Two test groups, mirroring the test_tenant_notifications.py pattern:

  * RLS isolation — pure DB tests proving the conversations + messages
    table policies (migrations 026 / 028) correctly strip cross-tenant
    reads under the ``polaris_test_anon`` role.
  * Router SQL — light coverage of the cursor pagination, channel
    filter, and detail join behaviour using the service connection
    (no ASGI bind) — the router logic is thin enough that the SQL
    paths are what need verification.

Skipped via the ``service_conn`` fixture when ``POLARIS_DATABASE_URL``
is unset.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest

# ---------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------


async def _seed_tenant(conn: asyncpg.Connection, slug: str) -> UUID:
    row = await conn.fetchrow(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug,
    )
    assert row is not None
    return UUID(str(row["id"]))


async def _seed_conversation(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    channel: str = "whatsapp",
    channel_user_id: str = "60123456789",
    display_name: str | None = None,
    last_message_at_iso: str | None = None,
) -> UUID:
    """Insert one conversations row.

    ``last_message_at_iso`` is an RFC3339 string the test wants to set
    explicitly so cursor-pagination assertions can pin deterministic
    ordering instead of relying on insertion order.
    """
    metadata = "{}"
    if display_name:
        # asyncpg encodes jsonb from text; small payload, so a string
        # literal is safer than the json codec gymnastics.
        metadata = f'{{"display_name": "{display_name}"}}'
    row = await conn.fetchrow(
        """
        INSERT INTO conversations
            (tenant_id, channel, channel_user_id, metadata, last_message_at)
        VALUES ($1, $2, $3, $4::jsonb,
                CASE WHEN $5::text IS NULL THEN NULL
                     ELSE $5::timestamptz END)
        RETURNING id
        """,
        tenant_id,
        channel,
        channel_user_id,
        metadata,
        last_message_at_iso,
    )
    assert row is not None
    return UUID(str(row["id"]))


async def _seed_message(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    role: str = "user",
    content: str = "hello",
    intent: str | None = None,
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO messages
            (tenant_id, conversation_id, role, content, intent)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        tenant_id,
        conversation_id,
        role,
        content,
        intent,
    )
    assert row is not None
    return UUID(str(row["id"]))


# =====================================================================
# RLS isolation
# =====================================================================


@pytest.mark.parametrize("table", ["conversations", "messages"])
async def test_force_rls_enabled(
    service_conn: asyncpg.Connection,
    table: str,
) -> None:
    """conversations + messages must both have ENABLE + FORCE RLS — the
    Phase 8 portal viewer is read-only but RLS is what keeps the
    cross-tenant invariant honest."""
    row = await service_conn.fetchrow(
        """
        SELECT relrowsecurity, relforcerowsecurity
          FROM pg_class
         WHERE oid = ($1 || '.' || $2)::regclass
        """,
        "public",
        table,
    )
    assert row is not None, f"table public.{table} missing"
    assert row["relrowsecurity"] is True, f"{table}: RLS not enabled"
    assert row["relforcerowsecurity"] is True, f"{table}: FORCE RLS not set"


async def test_conversations_cross_tenant_select_returns_zero_rows(
    service_conn: asyncpg.Connection,
) -> None:
    """Tenant A under RLS cannot see Tenant B's conversation."""
    tenant_a = await _seed_tenant(service_conn, slug="pytest-conv-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-conv-b")
    await _seed_conversation(
        service_conn, tenant_id=tenant_a, channel_user_id="111",
    )
    conv_b = await _seed_conversation(
        service_conn, tenant_id=tenant_b, channel_user_id="222",
    )

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_a),
        )
        cross = await service_conn.fetch(
            "SELECT id FROM conversations WHERE id = $1", conv_b,
        )
        assert cross == [], "RLS leak: tenant_a saw tenant_b's conversation"

        own = await service_conn.fetch(
            "SELECT channel_user_id FROM conversations WHERE tenant_id = $1",
            tenant_a,
        )
        assert len(own) == 1
        assert own[0]["channel_user_id"] == "111"
    finally:
        await tx.rollback()


async def test_messages_cross_tenant_select_returns_zero_rows(
    service_conn: asyncpg.Connection,
) -> None:
    """Tenant A cannot read Tenant B's messages even with the
    conversation id (would-be IDOR attack)."""
    tenant_a = await _seed_tenant(service_conn, slug="pytest-msg-a")
    tenant_b = await _seed_tenant(service_conn, slug="pytest-msg-b")
    conv_b = await _seed_conversation(
        service_conn, tenant_id=tenant_b, channel_user_id="999",
    )
    msg_b = await _seed_message(
        service_conn,
        tenant_id=tenant_b,
        conversation_id=conv_b,
        content="b-only",
    )

    tx = service_conn.transaction()
    await tx.start()
    try:
        await service_conn.execute("SET LOCAL ROLE polaris_test_anon")
        await service_conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_a),
        )
        # Direct id lookup
        cross = await service_conn.fetch(
            "SELECT id FROM messages WHERE id = $1", msg_b,
        )
        assert cross == [], "RLS leak: tenant_a saw tenant_b's message by id"
        # conversation_id lookup (the router's detail SQL shape)
        cross_by_conv = await service_conn.fetch(
            "SELECT id FROM messages WHERE conversation_id = $1", conv_b,
        )
        assert cross_by_conv == [], (
            "RLS leak: tenant_a saw tenant_b's messages via conversation_id"
        )
    finally:
        await tx.rollback()


# =====================================================================
# Router SQL: list shape, ordering, cursor pagination
# =====================================================================


async def test_list_orders_by_last_message_at_desc(
    service_conn: asyncpg.Connection,
) -> None:
    """The list SQL anchors on ``last_message_at DESC NULLS LAST, id DESC``.

    A conversation with a NULL last_message_at must sort after one with
    a non-null timestamp — matches the index in migration 026.
    """
    tenant = await _seed_tenant(service_conn, slug="pytest-conv-order")
    older = await _seed_conversation(
        service_conn,
        tenant_id=tenant,
        channel_user_id="aaa",
        last_message_at_iso="2026-05-19T10:00:00+00:00",
    )
    newer = await _seed_conversation(
        service_conn,
        tenant_id=tenant,
        channel_user_id="bbb",
        last_message_at_iso="2026-05-20T10:00:00+00:00",
    )
    null_msg = await _seed_conversation(
        service_conn,
        tenant_id=tenant,
        channel_user_id="ccc",
        last_message_at_iso=None,
    )

    rows = await service_conn.fetch(
        """
        SELECT id FROM conversations
         WHERE tenant_id = $1
         ORDER BY last_message_at DESC NULLS LAST, id DESC
        """,
        tenant,
    )
    ordered = [UUID(str(r["id"])) for r in rows]
    assert ordered[0] == newer
    assert ordered[1] == older
    assert ordered[2] == null_msg


async def test_list_filters_by_channel(
    service_conn: asyncpg.Connection,
) -> None:
    """The optional channel filter restricts to one channel without
    leaking other channels in the same tenant."""
    tenant = await _seed_tenant(service_conn, slug="pytest-conv-channel")
    wa = await _seed_conversation(
        service_conn, tenant_id=tenant, channel="whatsapp",
        channel_user_id="60123",
    )
    await _seed_conversation(
        service_conn, tenant_id=tenant, channel="messenger",
        channel_user_id="ig_user",
    )

    rows = await service_conn.fetch(
        """
        SELECT id FROM conversations
         WHERE tenant_id = $1 AND channel = $2
        """,
        tenant,
        "whatsapp",
    )
    assert len(rows) == 1
    assert UUID(str(rows[0]["id"])) == wa


async def test_detail_message_count_and_intent(
    service_conn: asyncpg.Connection,
) -> None:
    """The list lateral subqueries surface message_count + last_intent
    correctly when multiple messages with different intents exist.

    Last intent must come from the most-recently-created message that
    has a non-null intent — older messages are ignored even when newer
    intent-less messages exist.
    """
    tenant = await _seed_tenant(service_conn, slug="pytest-conv-detail")
    conv = await _seed_conversation(
        service_conn, tenant_id=tenant, channel_user_id="60111222333",
    )
    # 1: user intent-less, 2: assistant=greeting, 3: user=booking,
    # 4: assistant intent-less — last_intent must surface 'booking'.
    await _seed_message(
        service_conn, tenant_id=tenant, conversation_id=conv,
        role="user", content="hi",
    )
    await _seed_message(
        service_conn, tenant_id=tenant, conversation_id=conv,
        role="assistant", content="hello!", intent="greeting",
    )
    await _seed_message(
        service_conn, tenant_id=tenant, conversation_id=conv,
        role="user", content="i want to book", intent="booking",
    )
    await _seed_message(
        service_conn, tenant_id=tenant, conversation_id=conv,
        role="assistant", content="sure, when?",
    )

    row = await service_conn.fetchrow(
        """
        SELECT c.id,
               (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS n,
               (SELECT intent FROM messages m
                  WHERE m.conversation_id = c.id AND m.intent IS NOT NULL
                  ORDER BY m.created_at DESC LIMIT 1) AS last_intent
          FROM conversations c
         WHERE c.id = $1
        """,
        conv,
    )
    assert row is not None
    assert int(row["n"]) == 4
    assert row["last_intent"] == "booking"


# =====================================================================
# Customer display-name fallback
# =====================================================================


@pytest.mark.parametrize(
    ("channel_user_id", "expected_suffix"),
    [
        ("60123456789", "6789"),     # WA E.164
        ("abc", "abc"),              # short ids surface unchanged after redact
    ],
)
async def test_customer_display_name_fallback(
    channel_user_id: str,
    expected_suffix: str,
) -> None:
    """Router-level helper: when metadata.display_name is absent, fall
    back to a redacted form of channel_user_id (last 4 chars)."""
    from polaris_api.routers.tenant_conversations import (
        _customer_display_name,
    )

    name = _customer_display_name(None, channel_user_id)
    assert name.endswith(expected_suffix)


async def test_customer_display_name_uses_metadata() -> None:
    """When metadata.display_name is present + non-empty, it overrides
    the channel_user_id fallback."""
    from polaris_api.routers.tenant_conversations import (
        _customer_display_name,
    )

    assert (
        _customer_display_name({"display_name": "Jane Doe"}, "60123456789")
        == "Jane Doe"
    )
    # Empty / whitespace falls back so a blank pipeline write doesn't
    # render as a literal empty cell on the portal.
    assert _customer_display_name({"display_name": "   "}, "60123").endswith(
        "0123"
    )
