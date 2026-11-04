"""Meta API usage telemetry — writer for the System Health Meta tile.

Parses Meta's ``X-Business-Use-Case-Usage`` response header (captured by the
pure-HTTP meta_whatsapp client and handed here via an injected sink) and
persists a headroom row into ``meta_api_usage`` (migration 085).

The header is JSON keyed by business account id, each value a list of usage
records, e.g.::

    {"<biz_id>": [{"type": "messages", "call_count": 12, "total_cputime": 3,
                   "total_time": 7, "estimated_time_to_regain_access": 0}]}

call_count / total_cputime / total_time are percentages (0–100) of the rolling
window consumed. We take the worst (max) across all records as the binding
constraint and store ``calls_remaining_pct = 100 - that``.

Everything here is best-effort: telemetry MUST NOT affect or slow the send
path, so every public coroutine swallows its own errors.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from polaris_api.db.pools import get_service_pool

logger = logging.getLogger(__name__)

# Percentage-of-window-consumed fields in each usage record.
_USAGE_PCT_KEYS = ("call_count", "total_cputime", "total_time")


def parse_usage_pct(raw_header: str) -> float | None:
    """Return remaining headroom percentage (0–100), or None if unparseable.

    Pure function (no I/O) so it is unit-testable in isolation.
    """
    try:
        data: Any = json.loads(raw_header)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    max_consumed = 0.0
    found = False
    for entries in data.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in _USAGE_PCT_KEYS:
                value = entry.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    found = True
                    max_consumed = max(max_consumed, float(value))
    if not found:
        return None
    return max(0.0, 100.0 - min(max_consumed, 100.0))


async def record_usage(raw_header: str, surface: str = "whatsapp") -> None:
    """Best-effort persist of one usage observation. Never raises."""
    try:
        pct = parse_usage_pct(raw_header)
        if pct is None:
            return
        pool = get_service_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO meta_api_usage (surface, calls_remaining_pct, raw) "
                "VALUES ($1, $2, $3::jsonb)",
                surface,
                pct,
                raw_header,
            )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break sends
        logger.debug("meta usage record skipped: %s", exc)
