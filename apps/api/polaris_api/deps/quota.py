"""Quota-enforcement FastAPI dependency — Phase 10 PR-1.

Adds ``X-Quota-Used`` / ``X-Quota-Limit`` response headers to every
guarded endpoint.  Raises HTTP 429 when the tenant has exceeded their
monthly bot_replies hard cap; adds ``X-Quota-Warning`` header at the
80 % soft cap.

Usage — drop into any mutation endpoint whose execution consumes
bot-reply quota::

    from polaris_api.deps.quota import enforce_quota

    @router.post("/{id}/schedule")
    async def schedule_broadcast(
        ...
        _quota: Annotated[None, Depends(enforce_quota)],
    ) -> ...:
        ...

The dependency is intentionally side-effect-free: it never increments
the counter (that happens at pipeline step 12b after a successful Meta
send) — it only reads the current period value and sets headers.
"""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Response, status

from polaris_api.deps.tenant import TenantContext, get_request_conn, get_tenant_context
from polaris_api.redis_pool import get_redis
from polaris_api.services.quota import check_quota


async def enforce_quota(
    response: Response,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> None:
    """FastAPI dependency — check bot_replies quota and set response headers.

    Response headers always present on guarded endpoints:
      ``X-Quota-Used``    — replies consumed this billing period
      ``X-Quota-Limit``   — hard cap for this billing period

    Response header present when usage ≥ 80 %:
      ``X-Quota-Warning`` — ``approaching_limit``

    Raises:
      HTTP 429  when ``used > cap`` — body includes ``code``,
                ``used``, ``limit``.  ``Retry-After: 86400`` hints
                the client to retry tomorrow (counters reset monthly
                but 24 h is a safe conservative hint).
    """
    redis = get_redis()
    qs = await check_quota(conn, redis, tenant_id=ctx.tenant_id)

    response.headers["X-Quota-Used"] = str(qs.current_count)
    response.headers["X-Quota-Limit"] = str(qs.period_cap)

    if qs.over_cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "QUOTA_EXCEEDED",
                "used": qs.current_count,
                "limit": qs.period_cap,
            },
            headers={
                "X-Quota-Used": str(qs.current_count),
                "X-Quota-Limit": str(qs.period_cap),
                "Retry-After": "86400",
            },
        )
    if qs.at_soft_cap:
        response.headers["X-Quota-Warning"] = "approaching_limit"
