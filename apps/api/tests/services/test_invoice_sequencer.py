from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeConn:
    """Stub asyncpg connection for the invoice_sequence upsert.

    The real ``invoice_sequence`` table (migration 008) has a composite
    PK ``(invoice_type, yy, mm)`` and a ``next_index`` column; the
    INSERT … ON CONFLICT DO UPDATE returns ``next_index``. Quotes reuse
    the same table with ``invoice_type = 'Q'`` (migration 054 §c).
    """

    def __init__(self, *, initial_index: int = 0) -> None:
        self._index = initial_index

    def transaction(self) -> Any:
        return self

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO invoice_sequence" in sql:
            self._index += 1
            return {"next_index": self._index}
        return None


async def test_next_invoice_number_monthly() -> None:
    import polaris_api.services.billing.invoice_sequencer as seq_mod
    from polaris_api.services.billing.invoice_sequencer import next_invoice_number

    # Patch _year_month_myt to return a fixed value for determinism.
    original = seq_mod._year_month_myt
    seq_mod._year_month_myt = lambda: "2605"  # type: ignore[assignment]
    try:
        conn = _FakeConn(initial_index=0)
        result = await next_invoice_number(conn, "monthly")
        # No dash between YYMM and index per ADR-028 + the DB CHECK
        # constraint (^POL-[MA]-[0-9]{2}[0-9]{2}[0-9]+$).
        assert result == "POL-M-26050001"
    finally:
        seq_mod._year_month_myt = original


async def test_next_invoice_number_annual() -> None:
    import polaris_api.services.billing.invoice_sequencer as seq_mod
    from polaris_api.services.billing.invoice_sequencer import next_invoice_number

    original = seq_mod._year_month_myt
    seq_mod._year_month_myt = lambda: "2605"  # type: ignore[assignment]
    try:
        conn = _FakeConn(initial_index=2)
        result = await next_invoice_number(conn, "annual")
        assert result == "POL-A-26050003"
    finally:
        seq_mod._year_month_myt = original


async def test_next_quote_number() -> None:
    import polaris_api.services.billing.invoice_sequencer as seq_mod
    from polaris_api.services.billing.invoice_sequencer import next_quote_number

    original = seq_mod._year_month_myt
    seq_mod._year_month_myt = lambda: "2605"  # type: ignore[assignment]
    try:
        conn = _FakeConn(initial_index=0)
        result = await next_quote_number(conn)
        assert result == "POL-Q-26050001"
        assert result.startswith("POL-Q-")
    finally:
        seq_mod._year_month_myt = original


async def test_sequential_numbers_are_distinct() -> None:
    import polaris_api.services.billing.invoice_sequencer as seq_mod
    from polaris_api.services.billing.invoice_sequencer import next_invoice_number

    original = seq_mod._year_month_myt
    seq_mod._year_month_myt = lambda: "2605"  # type: ignore[assignment]
    try:
        conn = _FakeConn(initial_index=0)
        n1 = await next_invoice_number(conn, "monthly")
        n2 = await next_invoice_number(conn, "monthly")
        assert n1 != n2
        assert n1 == "POL-M-26050001"
        assert n2 == "POL-M-26050002"
    finally:
        seq_mod._year_month_myt = original
