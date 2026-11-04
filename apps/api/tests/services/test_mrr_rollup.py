"""MRR rollup service tests — Phase 9 PR-11; persistence rewrite 2026-06-05.

The trend now lives in ``platform_mrr_snapshots`` (migration 093) instead of
module globals. These tests use a single stateful fake connection
(``_FakeConn``) that emulates that table across calls, so the "DB" is the
shared state — mirroring real behaviour where multiple processes share one
table.

Coverage:
  * SUM computation → correct mrr_myr; NULL → 0.0
  * alert_band green / amber / red at the static band boundaries
  * compute_mrr is read-only (writes NO snapshot)
  * compute_and_persist appends exactly one snapshot per call
  * trend accumulates + is capped to 30
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

from polaris_api.services import mrr_rollup  # noqa: E402

# ---------------------------------------------------------------------------
# Stateful fake connection emulating platform_mrr_snapshots + tenant_billing.
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d[key]


class _FakeConn:
    """Emulates the snapshots table (shared, persistent) + the SUM query."""

    def __init__(self, total_mrr: float | None = 0.0) -> None:
        self.total = total_mrr
        self.rows: list[dict[str, Any]] = []  # chronological
        self._seq = 0

    def set_mrr(self, value: float | None) -> None:
        self.total = value

    async def fetchval(self, sql: str, *args: Any) -> Any:
        s = " ".join(sql.split())
        if "SUM(tb.mrr_myr)" in s:  # ADR-066: query now joins tenants to exclude is_demo
            return self.total
        if s.startswith("INSERT INTO platform_mrr_snapshots"):
            self._seq += 1
            ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=self._seq)
            self.rows.append(
                {"mrr_myr": float(args[0]), "alert_band": args[1], "computed_at": ts}
            )
            return ts
        return None

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
        s = " ".join(sql.split())
        if "FROM platform_mrr_snapshots" in s:
            limit = int(args[0]) if args else 30
            newest_first = list(reversed(self.rows))[:limit]
            return [_FakeRecord(r) for r in newest_first]
        return []


# ---------------------------------------------------------------------------
# Band computation — amber at 90% of the ~RM 41,667 ceiling, red at the
# ceiling itself (see module docstring / constants).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mrr", "band"),
    [
        (10_000.0, "green"),
        (30_000.0, "green"),
        (37_500.0, "amber"),
        (40_000.0, "amber"),
        (41_666.67, "red"),
        (50_000.0, "red"),
        (0.0, "green"),
    ],
)
async def test_band_thresholds(mrr: float, band: str) -> None:
    result = await mrr_rollup.compute_and_persist(_FakeConn(mrr))
    assert result["alert_band"] == band
    assert result["mrr_myr"] == pytest.approx(mrr)


async def test_null_mrr_treated_as_zero() -> None:
    result = await mrr_rollup.compute_and_persist(_FakeConn(None))
    assert result["mrr_myr"] == pytest.approx(0.0)
    assert result["alert_band"] == "green"


async def test_result_includes_required_keys() -> None:
    result = await mrr_rollup.compute_and_persist(_FakeConn(5_000.0))
    assert {"mrr_myr", "alert_band", "computed_at", "trend_30d"} <= result.keys()


# ---------------------------------------------------------------------------
# Read vs persist
# ---------------------------------------------------------------------------


async def test_compute_mrr_is_read_only() -> None:
    """compute_mrr returns the live band but writes NO snapshot row."""
    conn = _FakeConn(40_000.0)
    result = await mrr_rollup.compute_mrr(conn)
    assert result["alert_band"] == "amber"
    assert conn.rows == []  # nothing persisted


async def test_compute_and_persist_appends_one_row() -> None:
    conn = _FakeConn(5_000.0)
    await mrr_rollup.compute_and_persist(conn)
    assert len(conn.rows) == 1


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


async def test_trend_accumulates_across_calls() -> None:
    conn = _FakeConn(5_000.0)
    await mrr_rollup.compute_and_persist(conn)
    await mrr_rollup.compute_and_persist(conn)
    result = await mrr_rollup.compute_and_persist(conn)
    assert len(result["trend_30d"]) == 3


async def test_trend_capped_at_30() -> None:
    conn = _FakeConn(5_000.0)
    for _ in range(35):
        await mrr_rollup.compute_and_persist(conn)
    result = await mrr_rollup.compute_and_persist(conn)
    assert len(result["trend_30d"]) == 30


async def test_get_latest_snapshot_none_when_empty() -> None:
    assert await mrr_rollup.get_latest_snapshot(_FakeConn(5_000.0)) is None


async def test_get_latest_snapshot_after_persist() -> None:
    conn = _FakeConn(5_000.0)
    await mrr_rollup.compute_and_persist(conn)
    latest = await mrr_rollup.get_latest_snapshot(conn)
    assert latest is not None
    assert latest["mrr_myr"] == pytest.approx(5_000.0)
    assert latest["alert_band"] == "green"
