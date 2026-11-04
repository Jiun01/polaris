"""Tenant-facing follow-up config API — Phase 13 follow-ups PR-1.

Mounted at ``/api/tenant/followups``. Surfaces the single follow-up toggle
the portal Settings screen reads + writes: whether AI re-engagement
follow-ups are sent for this tenant's quiet WhatsApp conversations.

Quiet hours + timing are Polaris defaults (``services/followups/timing.py``
— 22:00–08:00 tenant-local, 12–23h after last inbound). No tenant knobs at
v1: activate and the system does the humane thing. Room to grow
(``max_per_episode``, quiet override, tone) without a migration — it's a
JSONB blob.

Storage: ``tenants.settings.followup_config`` JSONB, mirroring
``booking_config`` (small flat blob; RLS on ``tenants`` already gates it;
no new table).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-followups"])


class FollowupConfig(BaseModel):
    """Tenant follow-up knobs. One field at v1 (the toggle)."""

    enabled: bool = Field(
        default=False,
        description=(
            "When true, send one AI re-engagement follow-up to a customer "
            "who goes quiet inside the WhatsApp 24h window (no handoff, no "
            "reply), at a humane tenant-local time."
        ),
    )


def _config_from_settings(settings_jsonb: Any) -> FollowupConfig:
    """Lift ``tenants.settings.followup_config`` into typed shape; defaults
    when absent. Mirrors ``tenant_bookings._config_from_settings``."""
    if isinstance(settings_jsonb, str):
        try:
            settings_jsonb = json.loads(settings_jsonb)
        except json.JSONDecodeError:
            settings_jsonb = {}
    raw = (
        settings_jsonb.get("followup_config") or {}
        if isinstance(settings_jsonb, dict)
        else {}
    )
    if not isinstance(raw, dict):
        raw = {}  # corrupt scalar (e.g. `true`) → defaults, not a 500
    return FollowupConfig.model_validate(raw)


@router.get(
    "/config",
    response_model=FollowupConfig,
    summary="Read the calling tenant's follow-up configuration",
)
async def get_config(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> FollowupConfig:
    """Return the saved config or defaults (disabled) when nothing is set."""
    row = await conn.fetchrow(
        "SELECT settings FROM tenants WHERE id = $1", ctx.tenant_id,
    )
    if row is None:
        return FollowupConfig()
    return _config_from_settings(row["settings"])


@router.put(
    "/config",
    response_model=FollowupConfig,
    status_code=status.HTTP_200_OK,
    summary="Replace the calling tenant's follow-up configuration",
)
async def put_config(
    body: FollowupConfig,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> FollowupConfig:
    """Replace the ``followup_config`` blob inside ``tenants.settings``.

    Uses ``jsonb_set`` so other settings keys stay intact. Pass a dict so
    asyncpg serialises a JSONB object (not a quoted JSON string — same
    footgun documented in ``tenant_bookings.put_config``).
    """
    payload = body.model_dump(mode="json")
    await conn.execute(
        """
        UPDATE tenants
        SET settings = jsonb_set(
            CASE WHEN jsonb_typeof(settings) = 'object' THEN settings ELSE '{}'::jsonb END,
            '{followup_config}',
            $1::jsonb,
            true
        ),
        updated_at = now()
        WHERE id = $2
        """,
        payload,
        ctx.tenant_id,
    )
    logger.info(
        "tenant_followups.config_saved tenant=%s enabled=%s",
        ctx.tenant_id,
        body.enabled,
    )
    return body
