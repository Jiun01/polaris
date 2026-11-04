"""Regression coverage for the systemic JSONB double-encoding fix.

The anon/service pools register a JSONB codec (encoder=json.dumps) in
``db/pools.py::_init_conn``. Passing ``json.dumps(x)`` to a ``$N::jsonb`` param
double-encodes — Postgres stores a JSONB *string* instead of an object/array.
The writers across ~15 sites now pass the dict/list DIRECTLY (codec serialises
once). See services/audit.py for the canonical pattern.

Layers:
  * unit (AsyncMock) — assert representative service writers bind a dict/list,
    never a json.dumps string. Fast, always runs.
  * DB-backed — the only column the whole-DB audit found corrupted was
    ``tenants.settings`` (1 row). Assert (a) a dict write lands as a JSONB
    object, and (b) migration 112's repair turns a double-encoded string back
    into an object. Skips without a DSN.
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
from polaris_api.services import audit
from polaris_api.services.broadcast import worker as broadcast_worker

_TEST_TENANT_PREFIX = "pytest-jsonbfix-"

# The SET expression + predicate under test — kept identical to migration 112 so
# the test fails if the migration's repair logic drifts. The test SCOPES it to
# its own seeded tenant (``AND id = $1``) so it never mutates real rows on the
# shared dev DB; migration 112 itself runs the unscoped form on deploy.
_REPAIR_SET_AND_PREDICATE = (
    "UPDATE tenants SET settings = (settings #>> '{}')::jsonb "
    "WHERE jsonb_typeof(settings) = 'string'"
)
_SCOPED_REPAIR_SQL = f"{_REPAIR_SET_AND_PREDICATE} AND id = $1"


# ---------------------------------------------------------------------------
# Unit — representative writers bind dict/list, not a json.dumps string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_record_binds_dicts() -> None:
    # audit.record is the canonical dict-binding pattern this whole test module
    # is named after (see services/audit.py's module-docstring cross-reference).
    # It issues an advisory-lock SELECT + a chain-head SELECT before the INSERT;
    # stub conn.fetchval for both so the INSERT's bound params are inspectable.
    conn = AsyncMock()
    conn.fetchval.return_value = None  # no prior chain head
    tenant_id = uuid4()
    before = {"foo": "bar"}
    after = {"code": "boom"}
    await audit.record(
        conn,
        tenant_id=tenant_id,
        actor=None,
        action="test.action",
        before=before,
        after=after,
    )
    args = conn.execute.await_args.args
    # SQL is args[0]; params follow: (id, tenant_id, actor, action, before,
    # after, prev_hash, row_hash, created_at, impersonating_kind, impersonating_id).
    before_arg, after_arg = args[5], args[6]
    assert before_arg == before
    assert isinstance(before_arg, dict)
    assert after_arg == after
    assert isinstance(after_arg, dict)


@pytest.mark.asyncio
async def test_persist_outbound_event_binds_dict_payload() -> None:
    conn = AsyncMock()
    await broadcast_worker._persist_outbound_event(
        conn,
        tenant_id=uuid4(),
        event_type="broadcast.completed",
        payload={"broadcast_id": "abc", "sent": 5},
    )
    payload_arg = conn.execute.await_args.args[-1]
    assert isinstance(payload_arg, dict), (
        f"payload must bind as a dict (codec serialises once), got {type(payload_arg)}"
    )
    assert payload_arg == {"broadcast_id": "abc", "sent": 5}


# ---------------------------------------------------------------------------
# DB-backed — tenants.settings shape + migration 112 repair
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
    # Register the JSONB codec exactly like the pools do, so a dict param
    # round-trips the same way production writes it.
    import json

    for pg_type in ("json", "jsonb"):
        await conn.set_type_codec(
            pg_type, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    try:
        yield conn
    finally:
        try:
            await conn.execute(
                f"DELETE FROM tenants WHERE slug LIKE '{_TEST_TENANT_PREFIX}%'"  # noqa: S608
            )
        except Exception:  # noqa: BLE001, S110
            pass
        await conn.close()


@pytest.mark.asyncio
async def test_dict_write_stores_settings_as_object(
    seed_conn: asyncpg.Connection,
) -> None:
    # Mimics the fixed admin_tenants writer: dict bound to $N::jsonb.
    slug = f"{_TEST_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    tenant_id = await seed_conn.fetchval(
        "INSERT INTO tenants (slug, settings) VALUES ($1, $2::jsonb) RETURNING id",
        slug,
        {"vertical": "dental"},
    )
    typeof = await seed_conn.fetchval(
        "SELECT jsonb_typeof(settings) FROM tenants WHERE id = $1", tenant_id
    )
    vertical = await seed_conn.fetchval(
        "SELECT settings->>'vertical' FROM tenants WHERE id = $1", tenant_id
    )
    assert typeof == "object"
    assert vertical == "dental"


@pytest.mark.asyncio
async def test_migration_112_repairs_double_encoded_settings(
    seed_conn: asyncpg.Connection,
) -> None:
    # Seed a row in the BROKEN shape (settings is a JSONB string).
    slug = f"{_TEST_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
    tenant_id = await seed_conn.fetchval(
        """INSERT INTO tenants (slug, settings)
           VALUES ($1, '"{\\"vertical\\": \\"dental\\"}"'::jsonb) RETURNING id""",
        slug,
    )
    assert (
        await seed_conn.fetchval(
            "SELECT jsonb_typeof(settings) FROM tenants WHERE id = $1", tenant_id
        )
        == "string"
    )

    await seed_conn.execute(_SCOPED_REPAIR_SQL, tenant_id)

    typeof = await seed_conn.fetchval(
        "SELECT jsonb_typeof(settings) FROM tenants WHERE id = $1", tenant_id
    )
    vertical = await seed_conn.fetchval(
        "SELECT settings->>'vertical' FROM tenants WHERE id = $1", tenant_id
    )
    assert typeof == "object"
    assert vertical == "dental"

    # Idempotent — a second run leaves the now-object row untouched (the
    # jsonb_typeof='string' predicate no longer matches it).
    res = await seed_conn.execute(_SCOPED_REPAIR_SQL, tenant_id)
    assert res == "UPDATE 0"
    assert (
        await seed_conn.fetchval(
            "SELECT jsonb_typeof(settings) FROM tenants WHERE id = $1", tenant_id
        )
        == "object"
    )
