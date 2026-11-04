"""invoice_overdue._flip_overdue_invoices — audit Batch B (B7-e).

Covers the bulk UPDATE ... RETURNING flip + the per-row sequential audit
write (the hash chain in services/audit.py forbids batching audit inserts).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from polaris_api.scheduled import invoice_overdue

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeTx:
    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


class _FakeConn:
    """fetch() returns a fixed set of flipped rows; audit writes are
    captured via a monkeypatched audit_record so we can assert order +
    per-row invocation without a real hash chain. transaction() counts
    per-audit-row transactions (advisory-lock scoping, review fix)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.fetch_calls: list[str] = []
        self.transaction_count = 0

    async def fetch(self, sql: str, *_args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append(sql)
        return self._rows

    def transaction(self) -> _FakeTx:
        self.transaction_count += 1
        return _FakeTx()


async def test_bulk_flip_returns_flip_count(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"id": uuid4(), "tenant_id": uuid4()},
        {"id": uuid4(), "tenant_id": uuid4()},
        {"id": uuid4(), "tenant_id": uuid4()},
    ]
    conn = _FakeConn(rows)

    audit_calls: list[dict[str, Any]] = []

    async def _fake_audit(_conn: Any, **kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(invoice_overdue, "audit_record", _fake_audit)

    flipped = await invoice_overdue._flip_overdue_invoices(conn)  # noqa: SLF001

    assert flipped == 3
    # Exactly ONE bulk UPDATE ... RETURNING statement — not one per row.
    assert len(conn.fetch_calls) == 1
    assert "RETURNING" in conn.fetch_calls[0]


async def test_audit_record_called_once_per_flipped_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hash-chain constraint: one audit_record call PER ROW, sequential
    (not batched) — this is why the flip is bulk but the audit isn't."""
    row_a = {"id": uuid4(), "tenant_id": uuid4()}
    row_b = {"id": uuid4(), "tenant_id": uuid4()}
    conn = _FakeConn([row_a, row_b])

    audit_calls: list[dict[str, Any]] = []

    async def _fake_audit(_conn: Any, **kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(invoice_overdue, "audit_record", _fake_audit)

    await invoice_overdue._flip_overdue_invoices(conn)  # noqa: SLF001

    assert len(audit_calls) == 2
    # One transaction per audit row — pg_advisory_xact_lock is tx-scoped,
    # so each audit_record must run inside its own transaction.
    assert conn.transaction_count == 2
    assert audit_calls[0]["tenant_id"] == row_a["tenant_id"]
    assert audit_calls[0]["action"] == "invoice.overdue"
    assert audit_calls[0]["before"] == {"status": "issued"}
    assert audit_calls[0]["after"] == {
        "status": "overdue", "invoice_id": str(row_a["id"]),
    }
    assert audit_calls[1]["tenant_id"] == row_b["tenant_id"]


async def test_zero_overdue_invoices_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([])
    audit_calls: list[dict[str, Any]] = []

    async def _fake_audit(_conn: Any, **kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(invoice_overdue, "audit_record", _fake_audit)

    flipped = await invoice_overdue._flip_overdue_invoices(conn)  # noqa: SLF001

    assert flipped == 0
    assert audit_calls == []
