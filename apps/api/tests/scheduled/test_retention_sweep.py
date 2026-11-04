"""retention_sweep._sweep_tenant — audit_logs is never swept.

audit_logs is an append-only, hash-chained ledger (ADR-030; migration 012
REVOKEs DELETE from polaris_service). The sweep must NEVER attempt to delete
from it — doing so would raise InsufficientPrivilegeError and break the chain
verifier. These tests pin that the messages + ingestion_requests deletes and
the retention.sweep audit row still run, and that audit_logs is left untouched
regardless of the configured (now deprecated) audit_log_retention_days value.

The _FakeConn raises on any audit_logs DELETE as a regression guard: if the
sweep ever attempts it again, the unhandled error fails these tests loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from polaris_api.scheduled import retention_sweep

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeConn:
    """Records executed statements; raises if audit_logs is ever DELETEd."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        if "audit_logs" in sql:
            # Regression guard — the sweep must never reach this.
            raise asyncpg.InsufficientPrivilegeError(
                "permission denied for table audit_logs"
            )
        return "DELETE 0"

    def deleted(self, table: str) -> bool:
        return any(f"DELETE FROM {table}" in sql for sql in self.executed)  # noqa: S608


def _row(audit_days: int = 90) -> dict[str, int]:
    return {
        "conversation_retention_days": 30,
        "audit_log_retention_days": audit_days,
        "ingestion_requests_retention_days": 14,
    }


@pytest.mark.parametrize("audit_days", [90, 365, 0])
async def test_audit_logs_never_swept(
    monkeypatch: pytest.MonkeyPatch, audit_days: int
) -> None:
    """For any audit_log_retention_days, audit_logs is never DELETEd and the
    rest of the sweep (messages, ingestion_requests, audit row) still runs."""
    conn = _FakeConn()
    audit_calls: list[dict[str, Any]] = []

    async def _fake_record(_conn: Any, **kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(retention_sweep, "audit_record", _fake_record)

    # Must NOT raise — the audit_logs DELETE is never attempted.
    await retention_sweep._sweep_tenant(
        conn,  # type: ignore[arg-type]
        uuid4(),
        _row(audit_days),  # type: ignore[arg-type]
        datetime.now(UTC),
    )

    assert conn.deleted("audit_logs") is False  # the whole point
    assert conn.deleted("messages") is True
    assert conn.deleted("ingestion_requests") is True
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "retention.sweep"
    # The (now deprecated) value is still recorded for transparency.
    assert audit_calls[0]["after"]["audit_log_retention_days"] == audit_days


# ── B7-b: chunked delete loop termination ───────────────────────────────────


class _BatchedFakeConn:
    """Simulates a table with a fixed row count, deleting in
    ``retention_sweep._DELETE_BATCH``-row batches per call — used to prove
    the chunk loop issues exactly ``ceil(total_rows / batch)`` DELETE
    statements and stops on the first short batch."""

    def __init__(self, total_rows: int) -> None:
        self.remaining = total_rows
        self.call_count = 0

    async def execute(self, _sql: str, *_args: Any) -> str:
        self.call_count += 1
        batch = retention_sweep._DELETE_BATCH  # noqa: SLF001
        take = min(batch, self.remaining)
        self.remaining -= take
        return f"DELETE {take}"


async def test_chunked_age_delete_terminates_on_short_batch() -> None:
    """25,000 rows at a 10,000-row batch size → 3 DELETE calls (10k, 10k,
    5k), stopping because the 3rd batch came back short."""
    conn = _BatchedFakeConn(total_rows=25_000)

    total = await retention_sweep._chunked_age_delete(  # noqa: SLF001
        conn,  # type: ignore[arg-type]
        table="messages",
        tenant_id=uuid4(),
        retention_days=30,
    )

    assert total == 25_000
    assert conn.call_count == 3


async def test_chunked_age_delete_zero_rows_issues_one_call() -> None:
    """An empty table still issues exactly one (short) DELETE — the loop
    always runs at least once."""
    conn = _BatchedFakeConn(total_rows=0)

    total = await retention_sweep._chunked_age_delete(  # noqa: SLF001
        conn,  # type: ignore[arg-type]
        table="ingestion_requests",
        tenant_id=uuid4(),
        retention_days=14,
    )

    assert total == 0
    assert conn.call_count == 1


async def test_chunked_tenant_delete_terminates_on_short_batch() -> None:
    """Same termination contract for the DSAR cascade's tenant-scoped
    (no age predicate) chunked delete."""
    conn = _BatchedFakeConn(total_rows=12_345)

    total = await retention_sweep._chunked_tenant_delete(  # noqa: SLF001
        conn,  # type: ignore[arg-type]
        table="messages",
        tenant_id=uuid4(),
    )

    assert total == 12_345
    assert conn.call_count == 2  # 10,000 + 2,345


# ── B7-b: per-tenant conn acquire (not one conn for the whole sweep) ────────


class _RecordingPool:
    """Fake asyncpg.Pool — records how many times acquire() was called and
    yields a fresh _FakeConn each time, so a test can assert the sweep
    doesn't hold ONE conn across every tenant."""

    def __init__(self, tenants_row: list[dict[str, Any]]) -> None:
        self._tenants_row = tenants_row
        self.acquire_count = 0
        self.conns: list[_FakeConn] = []

    def acquire(self) -> _RecordingAcquire:
        self.acquire_count += 1
        conn = _FakeConn()
        self.conns.append(conn)
        return _RecordingAcquire(conn, self._tenants_row)


class _RecordingAcquire:
    def __init__(self, conn: _FakeConn, tenants_row: list[dict[str, Any]]) -> None:
        self._conn = conn
        self._tenants_row = tenants_row

    async def __aenter__(self) -> Any:
        # The first acquire() (the snapshot read) needs `.fetch`; per-tenant
        # acquires only need `.execute` (already on _FakeConn). Attach a
        # `.fetch` shim lazily so both call shapes work on the same stub.
        async def _fetch(_sql: str, *_args: Any) -> list[dict[str, Any]]:
            return self._tenants_row

        self._conn.fetch = _fetch  # type: ignore[attr-defined]
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_sweep_job_acquires_fresh_conn_per_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit Batch B (B7-b): _sweep_job releases the snapshot conn before the
    per-tenant loop, then acquires ONE FRESH conn per tenant — not one conn
    held for the whole run. 3 tenants + 1 snapshot read = 4 acquire() calls."""
    tenant_rows = [
        {
            "tenant_id": uuid4(),
            "status": "active",
            "status_updated_at": datetime.now(UTC),
            "conversation_retention_days": 0,
            "audit_log_retention_days": 0,
            "ingestion_requests_retention_days": 0,
        }
        for _ in range(3)
    ]
    pool = _RecordingPool(tenant_rows)

    audit_calls: list[dict[str, Any]] = []

    async def _fake_record(_conn: Any, **kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(retention_sweep, "audit_record", _fake_record)

    await retention_sweep._sweep_job(pool)  # type: ignore[arg-type]

    # 1 acquire for the tenant-id snapshot + 1 acquire per tenant (3).
    assert pool.acquire_count == 4
    assert len(audit_calls) == 3
