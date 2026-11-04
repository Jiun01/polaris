"""Admin promotions read surface — platform-wide mirror (WS4).

Mounted at ``/api/admin/promotions``. Super-admin only (``require_platform_admin``);
service-pool (BYPASSRLS) so the operator sees every tenant's promotions.

Read-only by design — tenant-facing promotion CRUD lives in the portal
(``routers/tenant_promotions.py`` at ``/api/tenant/promotions``). This exists so
the admin ``/promotions`` page (previously a static stub) can show real data.
Optional ``?tenant_id=`` filter.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin import get_admin_service_conn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-promotions"])

_FORBID = ConfigDict(extra="forbid")


class _AdminPromotionRow(BaseModel):
    """One promotion row enriched with its owning tenant's slug."""

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    tenant_slug: str
    tenant_branch_id: UUID | None
    title: str
    discount_text: str
    is_active: bool
    valid_from: str
    valid_until: str
    created_at: str


@router.get("", response_model=list[_AdminPromotionRow])
async def list_promotions(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    tenant_id: Annotated[UUID | None, Query(description="Optional tenant filter")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[_AdminPromotionRow]:
    """Platform-wide promotions mirror (read-only). Newest first."""
    rows = await conn.fetch(
        """
        SELECT p.id, p.tenant_id, t.slug AS tenant_slug, p.tenant_branch_id,
               p.title, p.discount_text, p.is_active,
               p.valid_from, p.valid_until, p.created_at
          FROM tenant_promotions p
          JOIN tenants t ON t.id = p.tenant_id
         WHERE ($1::uuid IS NULL OR p.tenant_id = $1)
         ORDER BY p.created_at DESC
         LIMIT $2
        """,
        tenant_id,
        limit,
    )
    return [
        _AdminPromotionRow(
            id=r["id"],
            tenant_id=r["tenant_id"],
            tenant_slug=r["tenant_slug"],
            tenant_branch_id=r["tenant_branch_id"],
            title=r["title"],
            discount_text=r["discount_text"],
            is_active=r["is_active"],
            valid_from=r["valid_from"].isoformat(),
            valid_until=r["valid_until"].isoformat(),
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]
