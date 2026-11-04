"""Atomic invoice sequence numbering — Phase 9 PR-7.

Generates POL-{M|A}-YYMMINDEX numbers via an ON CONFLICT DO UPDATE
upsert so concurrent callers never race to the same index.

ADR-028: number format is POL-{M|A}-YYMMINDEX (zero-padded to 4 digits,
NO dash between YYMM and index). The format is enforced by a DB CHECK
constraint on tenant_invoices.invoice_number and sales_quotes.quote_code
(migration 008 / 054): ``^POL-[MA]-[0-9]{2}[0-9]{2}[0-9]+$`` and
``^POL-Q-[0-9]{2}[0-9]{2}[0-9]+$``.

The backing table is ``invoice_sequence`` (migration 008): composite
PK ``(invoice_type, yy, mm)`` -> ``next_index``. Sales quotes reuse the
SAME table with ``invoice_type = 'Q'`` (migration 054 §c), giving quotes
their own monotonic namespace per month without a second table.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg

_TZ_MYT = ZoneInfo("Asia/Kuala_Lumpur")


def _year_month_myt() -> str:
    """Return 'YYMM' in Asia/Kuala_Lumpur time."""
    return datetime.now(_TZ_MYT).strftime("%y%m")


async def _next_index(conn: asyncpg.Connection, invoice_type: str) -> tuple[str, int]:
    """Advance and return ``(year_month, next_index)`` for one sequence key.

    Uses an INSERT … ON CONFLICT DO UPDATE on the composite PK
    ``(invoice_type, yy, mm)`` so two concurrent callers for the same
    type+month always get distinct indices. ``year_month`` (YYMM) is
    derived from ``_year_month_myt()`` so tests can patch it for
    determinism.
    """
    year_month = _year_month_myt()
    yy = int(year_month[:2])
    mm = int(year_month[2:])

    row = await conn.fetchrow(
        """
        INSERT INTO invoice_sequence (invoice_type, yy, mm, next_index)
        VALUES ($1, $2, $3, 1)
        ON CONFLICT (invoice_type, yy, mm) DO UPDATE
          SET next_index = invoice_sequence.next_index + 1
        RETURNING next_index
        """,
        invoice_type,
        yy,
        mm,
    )
    assert row is not None
    return year_month, row["next_index"]


async def next_invoice_number(conn: asyncpg.Connection, billing_cycle: str) -> str:
    """Assign the next POL-{M|A}-YYMMINDEX number atomically.

    billing_cycle: 'monthly' → letter 'M', anything else → 'A'.

    Format: POL-M-YYMMINDEX (no dash between YYMM and index per ADR-028
    and the DB CHECK constraint). e.g. POL-M-26050001 for the first
    monthly invoice in May 2026.
    """
    invoice_type = "M" if billing_cycle == "monthly" else "A"
    year_month, index = await _next_index(conn, invoice_type)
    return f"POL-{invoice_type}-{year_month}{index:04d}"


async def next_quote_number(conn: asyncpg.Connection) -> str:
    """Assign the next POL-Q-YYMMINDEX number atomically.

    Quotes reuse the invoice_sequence table with ``invoice_type = 'Q'``
    (migration 054 §c) so quote indices sit in their own per-month
    namespace and don't collide with invoice indices.

    Format: POL-Q-YYMMINDEX (no dash between YYMM and index per ADR-028
    and the sales_quotes.quote_code CHECK constraint).
    """
    year_month, index = await _next_index(conn, "Q")
    return f"POL-Q-{year_month}{index:04d}"
