"""MRR rollup service — Phase 9 PR-11; persistence hardening 2026-06-05.

Computes ``SUM(tenant_billing.mrr_myr)`` across active tenants, maps it to an
alert_band (green / amber / red) for coarse-grained classification, and — on
the persisting path — writes one snapshot row to ``platform_mrr_snapshots``
(migration 093).

Persistence replaced the original in-memory ``deque`` (lost on restart, and
divergent across multiple API processes). The 30-day trend now lives in the
table, so it survives restarts and is correct across processes.

Read vs persist:
  * ``compute_mrr``        — read-only; live MRR + band, writes NO snapshot.
                             Used by ``GET /financials/mrr`` + the weekly digest.
  * ``compute_and_persist``— writes one snapshot. Used by the nightly cron job
                             + ``POST /financials/mrr/recompute``.
  * ``get_trend``          — last N persisted snapshots, chronological.
  * ``get_latest_snapshot``— most recent snapshot + trend, or None.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Band boundaries in MYR/month — static classification constants derived from
# the Malaysian SST registration ceiling (RM 500,000 annual ≈ RM 41,667/month).
_RED_THRESHOLD: float = 500_000 / 12
_AMBER_THRESHOLD: float = 0.9 * _RED_THRESHOLD

_TREND_LIMIT = 30


def _compute_band(mrr: float) -> str:
    """Map an MRR value to a band string."""
    if mrr >= _RED_THRESHOLD:
        return "red"
    if mrr >= _AMBER_THRESHOLD:
        return "amber"
    return "green"


def _iso(value: Any) -> str:
    """Render a TIMESTAMPTZ (datetime) as ISO-8601; pass through strings."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def _sum_active_mrr(conn: Any) -> float:
    """SUM(mrr_myr) across active, non-demo tenants. NULL (empty table) → 0.0.

    Joins ``tenants`` to exclude ``is_demo`` rows (ADR-066) — the demo brand
    tenants AND the ``polaris-internal`` gateway are all ``is_demo=true`` and
    must never count toward MRR.
    """
    raw = await conn.fetchval(
        "SELECT COALESCE(SUM(tb.mrr_myr), 0) "
        "FROM tenant_billing tb "
        "JOIN tenants t ON t.id = tb.tenant_id "
        "WHERE tb.status = 'active' AND NOT t.is_demo"
    )
    return float(raw) if raw is not None else 0.0


async def get_trend(conn: Any, *, limit: int = _TREND_LIMIT) -> list[dict[str, Any]]:
    """Return the last ``limit`` persisted snapshots in chronological order."""
    rows = await conn.fetch(
        "SELECT mrr_myr, alert_band, computed_at FROM platform_mrr_snapshots "
        "ORDER BY computed_at DESC, id DESC LIMIT $1",
        limit,
    )
    # Query is DESC (newest first); reverse to chronological for the UI.
    return [
        {
            "mrr_myr": float(r["mrr_myr"]),
            "alert_band": r["alert_band"],
            "computed_at": _iso(r["computed_at"]),
        }
        for r in reversed(rows)
    ]


async def compute_mrr(conn: Any) -> dict[str, Any]:
    """Read-only: live MRR + band from tenant_billing. Writes NO snapshot."""
    mrr = await _sum_active_mrr(conn)
    band = _compute_band(mrr)
    return {
        "mrr_myr": mrr,
        "alert_band": band,
        "computed_at": datetime.now(UTC).isoformat(),
    }


async def compute_and_persist(conn: Any) -> dict[str, Any]:
    """Compute MRR, persist a snapshot, return result.

    Returns:
        dict with keys: mrr_myr, alert_band, computed_at, trend_30d.
    """
    mrr = await _sum_active_mrr(conn)
    band = _compute_band(mrr)

    computed_at = await conn.fetchval(
        "INSERT INTO platform_mrr_snapshots (mrr_myr, alert_band) "
        "VALUES ($1, $2) RETURNING computed_at",
        mrr,
        band,
    )

    trend = await get_trend(conn)
    logger.info("mrr_rollup: persisted snapshot mrr_myr=%.2f band=%s", mrr, band)
    return {
        "mrr_myr": mrr,
        "alert_band": band,
        "computed_at": _iso(computed_at),
        "trend_30d": trend,
    }


async def get_latest_snapshot(conn: Any) -> dict[str, Any] | None:
    """Return the most recent persisted snapshot + trend, or None if empty."""
    trend = await get_trend(conn)
    if not trend:
        return None
    latest = trend[-1]
    return {**latest, "trend_30d": trend}
