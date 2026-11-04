"""Tenant website-widget config router — off-Meta channels M4.

Mounted at ``/api/tenant/widget``. Self-serve (all tiers): the tenant enables
the bubble, picks which channels appear + their order, and copies the embed
snippet. Editing here changes the live bubble WITHOUT re-embedding (the bubble
fetches config by key at render time).

  * ``GET /``  — current config (lazily creates a row + widget_key on first
                 call so the embed snippet is always available).
  * ``PUT /``  — update enabled / greeting / channels. Each channel ``target``
                 is validated against the chat-host allowlist (422 otherwise),
                 so a bad URL can never reach the public redirect.

Auth: portal-JWT via ``get_tenant_context``; RLS scopes the row to the tenant.
"""

from __future__ import annotations

import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from polaris_api.config import get_settings
from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services import widget as widget_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-widget"])

_FORBID = ConfigDict(extra="forbid")
_MAX_CHANNELS = 6


class WidgetChannelEntry(BaseModel):
    model_config = _FORBID
    kind: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=60)
    target: str = Field(min_length=1, max_length=512)


class WidgetConfigResponse(BaseModel):
    model_config = _FORBID
    widget_key: str
    enabled: bool
    greeting: str
    channels: list[WidgetChannelEntry]
    embed_snippet: str


class WidgetConfigUpdate(BaseModel):
    model_config = _FORBID
    enabled: bool
    greeting: str = Field(min_length=1, max_length=80)
    channels: list[WidgetChannelEntry] = Field(max_length=_MAX_CHANNELS)


def _embed_snippet(widget_key: str) -> str:
    base = get_settings().public_api_base_url.rstrip("/")
    return (
        f'<script src="{base}/widget.js" '
        f'data-polaris-widget="{widget_key}" async></script>'
    )


async def _ensure_row(conn: asyncpg.Connection, tenant_id: object) -> asyncpg.Record:
    """Return the tenant's widget row, creating it (with a fresh key) if absent."""
    row = await conn.fetchrow(
        "SELECT widget_key, enabled, greeting, channels "
        "FROM tenant_widget_config WHERE tenant_id = $1",
        tenant_id,
    )
    if row is not None:
        return row
    return await conn.fetchrow(
        """
        INSERT INTO tenant_widget_config (tenant_id, widget_key)
        VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO UPDATE SET updated_at = now()
        RETURNING widget_key, enabled, greeting, channels
        """,
        tenant_id,
        widget_service.gen_widget_key(),
    )


def _to_response(row: asyncpg.Record) -> WidgetConfigResponse:
    raw = row["channels"]
    channels = [
        WidgetChannelEntry(kind=e["kind"], label=e["label"], target=e["target"])
        for e in (raw if isinstance(raw, list) else [])
        if isinstance(e, dict)
    ]
    return WidgetConfigResponse(
        widget_key=row["widget_key"],
        enabled=row["enabled"],
        greeting=row["greeting"],
        channels=channels,
        embed_snippet=_embed_snippet(row["widget_key"]),
    )


@router.get("", response_model=WidgetConfigResponse)
async def get_widget(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> WidgetConfigResponse:
    """Current widget config; lazily provisions a key on first call."""
    row = await _ensure_row(conn, ctx.tenant_id)
    return _to_response(row)


@router.put("", response_model=WidgetConfigResponse)
async def update_widget(
    body: WidgetConfigUpdate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> WidgetConfigResponse:
    """Update config. Rejects any channel target outside the chat-host allowlist."""
    for entry in body.channels:
        if not widget_service.is_allowed_target(entry.target):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"target for '{entry.kind}' must be an https link to one of "
                    "t.me / wa.me / m.me / ig.me"
                ),
            )

    # Pass the Python list directly — pools.py registers the JSONB codec
    # (encoder=json.dumps), so json.dumps-ing here too would store a quoted JSON
    # *string* (jsonb_typeof='string') and trip the array CHECK constraint.
    channels = [e.model_dump() for e in body.channels]
    # Single atomic upsert — no SELECT-then-UPDATE race / NoneType deref. The
    # widget_key is set only on first insert; on conflict it's preserved.
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_widget_config
            (tenant_id, widget_key, enabled, greeting, channels)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (tenant_id) DO UPDATE
           SET enabled = EXCLUDED.enabled,
               greeting = EXCLUDED.greeting,
               channels = EXCLUDED.channels,
               updated_at = now()
        RETURNING widget_key, enabled, greeting, channels
        """,
        ctx.tenant_id,
        widget_service.gen_widget_key(),
        body.enabled,
        body.greeting,
        channels,
    )
    return _to_response(row)
