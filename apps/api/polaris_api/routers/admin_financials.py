"""Admin financials router — Phase 9 PR-11.

Endpoints:
  GET  /mrr           — latest rollup row + band + 30-day trend
  POST /mrr/recompute — manual trigger; runs the same logic as the
                        nightly job; returns fresh result

Mounted at /api/admin/financials by main.py.

Auth: require_platform_admin (same JWT gate as all other /api/admin/* routers).
DB:   get_admin_service_conn (BYPASSRLS service pool).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends

from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services import mrr_rollup
from polaris_api.services.admin import get_admin_service_conn

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-financials"])


@router.get("/mrr", summary="Latest MRR rollup + 30-day trend")
async def get_mrr(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """Return the live MRR + band plus the persisted 30-day trend.

    Read-only: this computes the headline MRR fresh from tenant_billing but
    does NOT write a snapshot — otherwise every admin page load would pollute
    the trend with a new datapoint. Snapshots are written only by the nightly
    cron job and POST /mrr/recompute. The 30-day trend is read from
    platform_mrr_snapshots (migration 093).
    """
    current = await mrr_rollup.compute_mrr(conn)
    trend = await mrr_rollup.get_trend(conn)
    result = {**current, "trend_30d": trend}
    logger.info(
        "admin_financials: GET /mrr mrr_myr=%.2f band=%s trend=%d",
        result["mrr_myr"],
        result["alert_band"],
        len(trend),
    )
    return result


@router.post("/mrr/recompute", summary="Manually recompute MRR rollup")
async def recompute_mrr(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """Force a fresh MRR computation and return the result.

    This runs the identical logic as the nightly cron job, but triggered
    manually by the platform admin (e.g. after bulk billing changes).
    """
    result = await mrr_rollup.compute_and_persist(conn)
    logger.info(
        "admin_financials: manual recompute triggered mrr_myr=%.2f band=%s",
        result["mrr_myr"],
        result["alert_band"],
    )
    return result
