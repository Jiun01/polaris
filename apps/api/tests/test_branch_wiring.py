"""WS3 — inbox->branch writer + expanded branch CRUD contract.

The channel->branch writer is the linchpin that was missing (nothing wrote
``tenant_channels.tenant_branch_id``). These cover its verify + 404 paths and
that the expanded branch upsert accepts the operational fields.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from polaris_api.deps.tenant import TenantContext
from polaris_api.routers import tenant_channels
from polaris_api.routers.tenant_channels import ChannelBranchUpdate
from polaris_shared_types import TenantBranchUpsert

_TENANT = uuid4()


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT, user_id=uuid4(), role="tenant_admin", plan_tier="pro", status="active"
    )


class _FakeConn:
    def __init__(self, *, branch_exists: bool = True, channel_updated: bool = True) -> None:
        self._branch_exists = branch_exists
        self._channel_updated = channel_updated
        self.saw_branch_verify = False

    async def fetchval(self, sql: str, *_args: Any) -> Any:
        if "FROM tenant_branches" in sql:
            self.saw_branch_verify = True
            return 1 if self._branch_exists else None
        if "UPDATE tenant_channels" in sql:
            return uuid4() if self._channel_updated else None
        return None


@pytest.mark.asyncio(loop_scope="function")
async def test_set_channel_branch_success() -> None:
    conn = _FakeConn()
    branch, channel = uuid4(), uuid4()
    res = await tenant_channels.set_channel_branch(
        channel, ChannelBranchUpdate(tenant_branch_id=branch), _ctx(), conn
    )
    assert res.id == channel
    assert res.tenant_branch_id == branch
    assert conn.saw_branch_verify is True


@pytest.mark.asyncio(loop_scope="function")
async def test_set_channel_branch_unknown_branch_404() -> None:
    conn = _FakeConn(branch_exists=False)
    with pytest.raises(HTTPException) as exc:
        await tenant_channels.set_channel_branch(
            uuid4(), ChannelBranchUpdate(tenant_branch_id=uuid4()), _ctx(), conn
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "branch_not_found"


@pytest.mark.asyncio(loop_scope="function")
async def test_set_channel_branch_unknown_channel_404() -> None:
    conn = _FakeConn(channel_updated=False)
    with pytest.raises(HTTPException) as exc:
        await tenant_channels.set_channel_branch(
            uuid4(), ChannelBranchUpdate(tenant_branch_id=None), _ctx(), conn
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "channel_not_found"


@pytest.mark.asyncio(loop_scope="function")
async def test_clear_channel_branch_skips_branch_verify() -> None:
    conn = _FakeConn()
    res = await tenant_channels.set_channel_branch(
        uuid4(), ChannelBranchUpdate(tenant_branch_id=None), _ctx(), conn
    )
    assert res.tenant_branch_id is None
    # Clearing shouldn't run the branch-ownership SELECT.
    assert conn.saw_branch_verify is False


def test_branch_upsert_accepts_operational_fields() -> None:
    b = TenantBranchUpsert(
        name="KL Office",
        business_hours={"mon": {"open": "09:00", "close": "18:00", "closed": False}},
        handoff_message_in_hours="An agent will reply shortly.",
        handoff_message_after_hours="We're closed; back at 9am.",
        ea_provider_id=7,
        ea_default_service_id=3,
    )
    assert b.ea_provider_id == 7
    assert b.business_hours is not None


def test_branch_upsert_rejects_bad_ea_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TenantBranchUpsert(name="KL", ea_provider_id=0)  # ge=1
