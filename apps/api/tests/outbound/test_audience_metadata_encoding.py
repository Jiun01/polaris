"""Regression coverage for the audience_members.metadata JSONB double-encode bug.

The anon/service pools register a JSONB codec (encoder=json.dumps) in
``db/pools.py::_init_conn``. Passing ``json.dumps(metadata)`` to a ``$N::jsonb``
param therefore double-encodes — Postgres stores a JSONB *string* (`"{}"`,
jsonb_typeof='string') instead of an object, silently breaking
``metadata->>'intent'`` filters. The fix passes the dict DIRECTLY (codec
serialises once).

Two layers:
  * unit (AsyncMock) — assert the writers bind a ``dict`` (not a ``str``) for the
    metadata param. Fast, always runs, directly guards against a json.dumps
    regression.
  * DB-backed — against a real codec-registered anon-pool connection, assert
    ``jsonb_typeof(metadata) = 'object'`` and key extraction works end-to-end.
    Skips without a DSN (matches the chatwoot persist test).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from polaris_api.config import get_settings
from polaris_api.db import pools as pool_module
from polaris_api.services.audience import importer
from polaris_api.services.audience.members import upsert_audience_member

_TEST_TENANT_PREFIX = "pytest-audmeta-"


# ---------------------------------------------------------------------------
# Unit — the writers must bind a dict, never a json.dumps string
# ---------------------------------------------------------------------------


def _last_call_args(mock: AsyncMock) -> tuple:
    return mock.execute.await_args.args


@pytest.mark.asyncio
async def test_upsert_member_binds_dict_metadata() -> None:
    conn = AsyncMock()
    await upsert_audience_member(
        conn,
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123456789",
        channel_user_name="Aisha",
        metadata={"intent": "vip"},
    )
    args = _last_call_args(conn)
    metadata_arg = args[-1]
    assert isinstance(metadata_arg, dict), (
        f"metadata must bind as a dict (codec serialises once), got {type(metadata_arg)}"
    )
    assert metadata_arg == {"intent": "vip"}


@pytest.mark.asyncio
async def test_upsert_member_none_metadata_binds_empty_dict() -> None:
    conn = AsyncMock()
    await upsert_audience_member(
        conn,
        tenant_id=uuid4(),
        channel="messenger",
        channel_user_id="PSID_1",
        channel_user_name=None,
        metadata=None,
    )
    assert _last_call_args(conn)[-1] == {}
    assert isinstance(_last_call_args(conn)[-1], dict)


def _parsed_row(**over: object) -> importer._ParsedRow:
    base: dict[str, object] = {
        "row_number": 1,
        "channel": "whatsapp",
        "channel_user_id": "60123456789",
        "channel_user_name": "Aisha",
        "phone_number_e164": "60123456789",
        "locale": "en",
        "tags": ["vip"],
        "opted_in_at": None,
        "metadata": {"intent": "vip"},
        "tenant_branch_id": None,
        "opt_in_source": "csv",
    }
    base.update(over)
    return importer._ParsedRow(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_importer_update_branch_binds_dict_metadata() -> None:
    # conn.execute returns "UPDATE 1" → an active row matched, no INSERT fires.
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    await importer._upsert_row(conn, tenant_id=uuid4(), row=_parsed_row())
    metadata_arg = conn.execute.await_args.args[-1]
    assert isinstance(metadata_arg, dict)
    assert metadata_arg == {"intent": "vip"}


@pytest.mark.asyncio
async def test_importer_insert_branch_binds_dict_metadata() -> None:
    # "UPDATE 0" → no active row → INSERT branch fires. The INSERT is the 2nd
    # execute; its last positional is the metadata dict.
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    await importer._upsert_row(conn, tenant_id=uuid4(), row=_parsed_row())
    assert conn.execute.await_count == 2
    insert_args = conn.execute.await_args_list[1].args
    metadata_arg = insert_args[-1]
    assert isinstance(metadata_arg, dict)
    assert metadata_arg == {"intent": "vip"}


# ---------------------------------------------------------------------------
# DB-backed — storage shape is a JSONB object, not a string
# ---------------------------------------------------------------------------


def _dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest.fixture
def configured_settings() -> Iterator[None]:
    get_settings.cache_clear()
    settings = get_settings()
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
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL to run this test")
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
                DELETE FROM audience_members WHERE tenant_id IN (
                  SELECT id FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'
                )
                """  # noqa: S608
            )
            await conn.execute(
                f"DELETE FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'"  # noqa: S608
            )
        except Exception:  # noqa: BLE001, S110
            pass
        await conn.close()


@pytest.mark.asyncio
async def test_upsert_stores_metadata_as_jsonb_object(
    seed_conn: asyncpg.Connection,
) -> None:
    slug = f"{_TEST_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    tenant_id = await seed_conn.fetchval(
        "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug
    )

    pool = pool_module.get_anon_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )
            await upsert_audience_member(
                conn,
                tenant_id=tenant_id,
                channel="whatsapp",
                channel_user_id="60199998888",
                channel_user_name="Aisha",
                metadata={"intent": "vip"},
            )
            typeof = await conn.fetchval(
                """
                SELECT jsonb_typeof(metadata) FROM audience_members
                 WHERE tenant_id = $1 AND channel_user_id = $2
                """,
                tenant_id,
                "60199998888",
            )
            intent = await conn.fetchval(
                """
                SELECT metadata->>'intent' FROM audience_members
                 WHERE tenant_id = $1 AND channel_user_id = $2
                """,
                tenant_id,
                "60199998888",
            )

    assert typeof == "object", f"metadata must be a JSONB object, got {typeof!r}"
    assert intent == "vip"
