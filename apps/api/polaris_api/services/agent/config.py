"""Load per-tenant ``AgentConfig`` from ``tenants.settings.agent_config``.

Mirrors the ``followup_config`` / ``booking_config`` read pattern. Fail-soft: any
missing / malformed config yields the disabled default, so a bad blob can never
break the inbound pipeline (the agent just stays off for that tenant).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg
from polaris_shared_types import AgentConfig


def parse_agent_config(blob: Any) -> AgentConfig:
    """Parse a raw ``agent_config`` JSONB blob (dict, JSON string, or None) into
    an ``AgentConfig``. Fail-soft to the disabled default so a malformed blob can
    never break the inbound pipeline. Used by the agent path against the value
    already carried in ``load_tenant_snapshot`` — no second query (M1)."""
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            return AgentConfig()
    if not isinstance(blob, dict):
        return AgentConfig()
    try:
        return AgentConfig.model_validate(blob)
    except Exception:  # noqa: BLE001 — malformed config → disabled default
        return AgentConfig()


async def load_agent_config(conn: asyncpg.Connection, tenant_id: UUID) -> AgentConfig:
    """Standalone read (used by tests / non-pipeline callers). The hot path reads
    ``agent_config`` from ``load_tenant_snapshot`` via ``parse_agent_config``."""
    row = await conn.fetchrow(
        "SELECT COALESCE(settings, '{}'::jsonb) AS settings FROM tenants WHERE id = $1",
        tenant_id,
    )
    if row is None:
        return AgentConfig()

    raw = row["settings"]
    if isinstance(raw, str):
        try:
            settings = json.loads(raw) or {}
        except (json.JSONDecodeError, ValueError):
            return AgentConfig()
    elif isinstance(raw, dict):
        settings = raw
    else:
        return AgentConfig()

    return parse_agent_config(settings.get("agent_config"))
