"""Read a tenant's ``booking_config`` from ``tenants.settings`` JSONB.

Booking knobs (reminder offset, quiet hours, confirmation/reminder template
names, timezone) live under ``tenants.settings.booking_config`` per ADR-049 —
there is no dedicated table. Several call sites need to read them off the
service pool (the EA webhook confirm path, the reminder worker, the internal
E2E seeder), so the parse lives here once instead of being re-inlined.

The read fails *soft*: a missing tenant, NULL/string-encoded settings, or a
malformed blob all return ``{}`` so callers fall back to their own defaults
rather than raising on a booking that otherwise confirmed fine.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg


async def _read_settings(
    conn: asyncpg.Connection, tenant_id: UUID
) -> dict[str, Any]:
    """Return ``tenants.settings`` as a dict (``{}`` if absent/malformed).

    asyncpg may hand back ``settings`` as a decoded dict or as a JSON string
    depending on the codec registered on the connection; both are handled.
    """
    row = await conn.fetchrow(
        "SELECT settings FROM tenants WHERE id = $1 LIMIT 1",
        tenant_id,
    )
    if row is None:
        return {}
    raw = row.get("settings")  # soft-fail if the projection ever changes
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


async def read_booking_config(
    conn: asyncpg.Connection, tenant_id: UUID
) -> dict[str, Any]:
    """Return ``tenants.settings.booking_config`` as a dict (``{}`` if absent)."""
    settings = await _read_settings(conn, tenant_id)
    booking_config = settings.get("booking_config")
    return booking_config if isinstance(booking_config, dict) else {}


async def read_ea_calendar(
    conn: asyncpg.Connection, tenant_id: UUID
) -> dict[str, Any]:
    """Return ``tenants.settings.ea_calendar`` (the EA mirror) as a dict.

    Returns ``{}`` when the tenant has never synced or the blob is malformed.
    The mirror is written by ``settings_sync.sync_ea_calendar`` and carries
    ``timezone`` (provider tz for in-chat slots) and ``working_plan`` (weekday →
    ``{start,end,breaks}`` or ``null`` when closed).
    """
    settings = await _read_settings(conn, tenant_id)
    ea_calendar = settings.get("ea_calendar")
    return ea_calendar if isinstance(ea_calendar, dict) else {}


__all__ = ["read_booking_config", "read_ea_calendar"]
