"""quota_flush._flush_counters — orphan counters never hit the FK.

Regression for the prod incident (2026-06-13 Chatwoot-native cutover): a tenant
was wiped from ``tenants`` while a live Redis ``bot_replies:*`` counter lingered.
The nightly flush kept INSERTing it into ``tenant_usage`` and failing the
``tenant_usage_tenant_id_fkey`` foreign key — logging ERROR every run.

``_flush_counters`` now pre-filters counter tenant_ids against ``tenants`` so an
orphan is *skipped* (counted, one aggregated WARNING) and never reaches the
write. These tests pin that behaviour with a fake pool whose ``tenant_usage``
INSERT raises a ForeignKeyViolationError if the orphan ever slips through.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from polaris_api.scheduled import quota_flush

pytestmark = pytest.mark.asyncio(loop_scope="function")

_PERIOD = "202606"


class _FakeConn:
    """Records executed SQL. ``known`` = tenant ids present in ``tenants``.

    audit Batch B (B7-d): the flush is now ONE bulk UNNEST INSERT + ONE bulk
    UNNEST UPDATE per run (not one INSERT+UPDATE pair per tenant), so
    per-tenant error isolation no longer exists — ``fail_bulk`` simulates a
    whole-batch write failure instead of a single-tenant one. The FK
    regression guard now asserts the bulk INSERT's tenant_id ARRAY never
    contains an orphan id (the pre-filter in ``_flush_counters`` must have
    already excluded it), rather than raising per-row.
    """

    def __init__(self, known: set[UUID], *, fail_bulk: bool = False) -> None:
        self._known = known
        self._fail_bulk = fail_bulk
        self.usage_writes: list[str] = []
        self.quota_writes: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, UUID]]:
        # _known_tenant_ids: SELECT id FROM tenants WHERE id = ANY($1::uuid[])
        candidates: list[UUID] = list(args[0])
        return [{"id": tid} for tid in candidates if tid in self._known]

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO tenant_usage" in sql:
            ids: list[str] = args[0]
            for id_str in ids:
                tenant_id = UUID(id_str)
                if tenant_id not in self._known:
                    # The whole point: an orphan must never reach the bulk
                    # INSERT's id array.
                    raise asyncpg.ForeignKeyViolationError(
                        'insert or update on table "tenant_usage" violates '
                        'foreign key constraint "tenant_usage_tenant_id_fkey"'
                    )
            if self._fail_bulk:
                raise asyncpg.PostgresError("simulated bulk write failure")
            self.usage_writes.extend(ids)
        elif "UPDATE tenant_quotas" in sql:
            self.quota_writes.extend(args[0])
        return "OK"

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


async def test_orphan_counter_skipped_no_fk_hit() -> None:
    """A counter whose tenant is absent from ``tenants`` is skipped, not
    INSERTed — so the FK is never violated and the run reports no error."""
    live = uuid4()
    orphan = uuid4()
    conn = _FakeConn(known={live})
    pool = _FakePool(conn)

    result = await quota_flush._flush_counters(
        pool,  # type: ignore[arg-type]
        {str(live): 9, str(orphan): 6},
        period=_PERIOD,
    )

    assert result.flushed == 1
    assert result.total_replies == 9
    assert result.errors == 0  # orphan must NOT be an error
    assert result.skipped == 1
    assert conn.usage_writes == [str(live)]  # orphan never written
    assert conn.quota_writes == [str(live)]


async def test_unparseable_tenant_id_skipped() -> None:
    """A non-UUID key segment is treated as an orphan and skipped without raising."""
    live = uuid4()
    conn = _FakeConn(known={live})
    pool = _FakePool(conn)

    result = await quota_flush._flush_counters(
        pool,  # type: ignore[arg-type]
        {str(live): 3, "not-a-uuid": 99},
        period=_PERIOD,
    )

    assert result.flushed == 1
    assert result.skipped == 1
    assert result.errors == 0
    assert conn.usage_writes == [str(live)]


async def test_all_orphans_nothing_written() -> None:
    """The incident's exact shape: every counter is an orphan → zero writes,
    zero errors, all skipped (was: errors=1 every night)."""
    orphan = uuid4()
    conn = _FakeConn(known=set())
    pool = _FakePool(conn)

    result = await quota_flush._flush_counters(
        pool,  # type: ignore[arg-type]
        {str(orphan): 6},
        period=_PERIOD,
    )

    assert result == quota_flush.FlushResult(
        flushed=0, errors=0, total_replies=0, skipped=1
    )
    assert conn.usage_writes == []


async def test_bulk_write_failure_counts_whole_batch_as_errors() -> None:
    """audit Batch B (B7-d): the flush is now ONE bulk statement per run, so
    a write failure fails the WHOLE known-tenant batch (per-tenant isolation
    traded for one round-trip — accepted per the audit; Redis counters are
    untouched and the next nightly run retries)."""
    ok = uuid4()
    also_ok = uuid4()
    conn = _FakeConn(known={ok, also_ok}, fail_bulk=True)
    pool = _FakePool(conn)

    result = await quota_flush._flush_counters(
        pool,  # type: ignore[arg-type]
        {str(ok): 4, str(also_ok): 7},
        period=_PERIOD,
    )

    assert result.flushed == 0
    assert result.errors == 2  # both known tenants counted, bulk-failed
    assert result.skipped == 0
    assert result.total_replies == 0
    assert conn.usage_writes == []  # the failed INSERT wrote nothing


async def test_bulk_flush_binds_ids_and_counts_as_parallel_arrays() -> None:
    """The two known tenants' ids/counts are passed as parallel arrays to
    the bulk UNNEST INSERT + UPDATE — proving the single-statement bind
    shape (not N separate per-tenant calls)."""
    ok = uuid4()
    also_ok = uuid4()
    conn = _FakeConn(known={ok, also_ok})
    pool = _FakePool(conn)

    result = await quota_flush._flush_counters(
        pool,  # type: ignore[arg-type]
        {str(ok): 4, str(also_ok): 7},
        period=_PERIOD,
    )

    assert result.flushed == 2
    assert result.errors == 0
    assert result.total_replies == 11
    assert sorted(conn.usage_writes) == sorted([str(ok), str(also_ok)])
    assert sorted(conn.quota_writes) == sorted([str(ok), str(also_ok)])
