"""Email channel provisioning — unit tests (DB-free).

Email is provisionable (migration 097 / ``_PROVISIONABLE_KINDS``) but, unlike the
Meta surfaces, stores NO outbound token (Chatwoot owns IMAP/SMTP). These tests
pin both halves: email passes the provisionable-kind gate + tier-cap gate, and
deprovision never touches ``tenant_secrets`` for it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.channels.provision import (
    TierCapError,
    deprovision_channel,
    provision_channel,
)

_TENANT = uuid4()


@pytest.mark.asyncio
async def test_email_is_provisionable_on_pro() -> None:
    conn = AsyncMock()
    # _check_tier_cap fetchrow: Pro tenant entitled to email.
    conn.fetchrow = AsyncMock(
        return_value={
            "plan_tier": "pro",
            "allowed_channel_kinds": ["whatsapp", "email"],
        }
    )
    channel_id = uuid4()
    conn.fetchval = AsyncMock(return_value=channel_id)

    result = await provision_channel(
        conn,
        tenant_id=_TENANT,
        channel_kind="email",
        connection_payload={"chatwoot_inbox_id": 6},
    )

    assert result.channel_kind == "email"
    assert result.channel_id == channel_id
    # INSERT positional args: (SQL, tenant_id, channel, chatwoot_inbox_id, ...).
    insert_args = conn.fetchval.await_args.args
    assert insert_args[2] == "email"
    assert insert_args[3] == 6


@pytest.mark.asyncio
async def test_email_rejected_on_starter() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={"plan_tier": "starter", "allowed_channel_kinds": ["whatsapp"]}
    )
    with pytest.raises(TierCapError):
        await provision_channel(
            conn,
            tenant_id=_TENANT,
            channel_kind="email",
            connection_payload={"chatwoot_inbox_id": 6},
        )


@pytest.mark.asyncio
async def test_deprovision_email_removes_no_secret() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=uuid4())  # DELETE ... RETURNING id

    result = await deprovision_channel(conn, tenant_id=_TENANT, channel_kind="email")

    assert result.channel_kind == "email"
    # No tenant_secrets.kind maps to email → nothing removed, no secret DELETE.
    assert result.secret_kind_removed is None
    conn.execute.assert_not_awaited()
