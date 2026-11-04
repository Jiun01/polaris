"""GA `/api/v1/audience/opted-out` exception — Phase 6 PR-4.

Single endpoint. The ONLY ``/api/v1/*`` route shipping at GA per
ADR-035 Amendment evening; the rest of the `/api/v1/*` surface is
v3.0 (tenant API key authentication ships then).

The auth is intentionally portal JWT, NOT tenant API key — the
endpoint surfaces under `/api/v1/*` for v3.0 forward-compat (so the
path doesn't move when programmatic API keys land), but the auth
mechanism is the same one the portal uses. This is documented
loudly in the OpenAPI summary so nobody assumes tenant API keys
exist at GA.

Implementation: forwards to ``services/audience/opt_out.list_opted_out``
so the `/api/tenant/...` and `/api/v1/...` views are identical bytes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Query
from polaris_shared_types import OptOutViewerPage

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.audience import opt_out

router = APIRouter(tags=["public-v1-audience"])


@router.get(
    "/opted-out",
    response_model=OptOutViewerPage,
    summary="(GA exception) Opt-out viewer — PORTAL JWT auth at GA",
    description=(
        "**Portal JWT at GA** (NOT tenant API key — tenant API keys "
        "are v3.0 per ADR-035 Amendment evening). Surfaces under "
        "`/api/v1/*` for v3.0 forward-compat so the path doesn't "
        "move when programmatic auth lands. Identical implementation "
        "to GET /api/tenant/audience/opted-out."
    ),
)
async def list_opted_out_v1(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    channel: str | None = None,
    opted_out_since: datetime | None = None,
    cursor: str | None = None,
    page_size: int = Query(default=200, ge=1, le=1000),
) -> OptOutViewerPage:
    return await opt_out.list_opted_out(
        conn,
        channel=channel,
        opted_out_since=opted_out_since,
        cursor=cursor,
        page_size=page_size,
    )
