"""Tenant config snapshot reader.

Generic tenant-config read used across the reply hot path (agent, classic
pipeline), email drafting, and follow-up scheduling. Originally lived in
``flow_runtime/repo.py`` (Phase 5 PR-4) but has zero flow-specific content —
it reads ``tenants.timezone/system_prompt/settings`` only. Relocated here
(audit Batch D, flow_runtime retirement) so non-flow callers don't import
from a flow-specific package for a plain tenant-config read.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg

# Canonical snapshot shape + NULL-safe defaults. The "no such tenant" branch
# returns exactly these; the populated branch overrides each key from the row.
# NOTE ``business_hours`` is re-set to a FRESH ``{}`` at every return site so
# callers never share (or accidentally mutate) this module-level dict.
_EMPTY_SNAPSHOT: dict[str, Any] = {
    "timezone": "UTC",
    "business_hours": {},
    "system_prompt": None,
    "persona_assistant_name": None,
    "persona_company_name": None,
    "email_draft_prompt": None,
    "agent_config": None,
    "persona_blocks": None,
    "plan_tier": "starter",
    "cohere_rerank_enabled": None,
}


async def load_tenant_snapshot(
    conn: asyncpg.Connection, tenant_id: UUID
) -> dict[str, Any]:
    """Pull the minimal tenant config the reply paths + persona need.

    M7 remediation (phase-5 code review): consolidated from the two
    duplicate definitions that previously lived in ``pipeline.py``
    (``_load_tenant_snapshot``) and ``tenant_flows.py`` (same name).
    Both call sites now import this function so the v2.1 wiring of
    ``business_hours`` only needs one update.

    H-3 audit fix (2026-05-19): now also returns ``system_prompt``,
    ``persona_assistant_name`` (derived from ``settings->>'assistant_name'``
    if set; otherwise NULL), ``persona_company_name`` (same pattern).
    Phase 4's pipeline.run() uses these to build a ``TenantPersona``
    so multi-tenant cutover doesn't leak "Pol / Polaris" branding
    into every reply.

    The ``settings`` JSONB column has existed since migration 002 —
    we use that for the assistant / company names instead of adding
    new columns. The portal will surface a settings form in Phase 8.

    At GA the runtime consumes ``timezone`` + ``business_hours``
    (ADR-019). The ``business_hours`` column doesn't ship in Phase 1
    yet (the migration declares it but no portal write path exists);
    we return ``{}`` as a NULL-safe fallback so ``business_hours_now()``
    resolves cleanly to ``False`` against an empty schedule rather
    than crashing the runtime.
    """
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(timezone, 'UTC') AS timezone,
            system_prompt,
            plan_tier,
            cohere_rerank_enabled,
            COALESCE(settings, '{}'::jsonb) AS settings
          FROM tenants
         WHERE id = $1
        """,
        tenant_id,
    )
    if row is None:
        return {**_EMPTY_SNAPSHOT, "business_hours": {}}
    # Defensive conversion: asyncpg.Record is dict-like but raises
    # KeyError on missing columns; some test fixtures return a partial
    # dict (e.g. ``{"plan_tier": "starter"}`` from shared mocks that
    # match the substring ``FROM tenants`` across multiple queries).
    # ``dict(row)`` then ``.get()`` keeps both happy without forcing
    # every test mock to enumerate the full snapshot schema.
    row_dict: dict[str, Any] = dict(row)
    raw_settings = row_dict.get("settings")
    settings_dict: dict[str, Any]
    if isinstance(raw_settings, str):
        try:
            settings_dict = json.loads(raw_settings) or {}
        except (json.JSONDecodeError, ValueError):
            settings_dict = {}
    elif isinstance(raw_settings, dict):
        settings_dict = raw_settings
    else:
        settings_dict = {}
    assistant_name = settings_dict.get("assistant_name")
    company_name = settings_dict.get("company_name")
    email_draft_prompt = settings_dict.get("email_draft_prompt")
    return {
        **_EMPTY_SNAPSHOT,
        "timezone": row_dict.get("timezone") or "UTC",
        "business_hours": {},
        "system_prompt": row_dict.get("system_prompt"),
        "persona_assistant_name": assistant_name if isinstance(assistant_name, str) else None,
        "persona_company_name": company_name if isinstance(company_name, str) else None,
        "email_draft_prompt": email_draft_prompt
        if isinstance(email_draft_prompt, str)
        else None,
        # Raw agent_config blob (ADR-063) — parsed by the agent path so the
        # tool-surface check reuses this one read instead of a second query.
        "agent_config": settings_dict.get("agent_config"),
        # Raw persona_blocks list (Agent-screen redesign) — parsed by both reply
        # paths into TenantPersona so the block editor drives the persona body.
        "persona_blocks": settings_dict.get("persona_blocks"),
        # audit Batch B (B1): plan_tier + cohere_rerank_enabled, so the
        # knowledge-gap capture gate and the classic-path RAG tier gate can
        # reuse this ONE snapshot read instead of issuing their own
        # `SELECT ... FROM tenants` per inbound message.
        "plan_tier": row_dict.get("plan_tier") or "starter",
        "cohere_rerank_enabled": row_dict.get("cohere_rerank_enabled"),
    }


__all__ = ["load_tenant_snapshot"]
