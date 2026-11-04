"""Service-pool connection dep for admin routes — Phase 3 PR-4.

Admin routes are platform-scope, not tenant-scope: there is no JWT
``tenant_id`` claim, and the caller is the platform itself
(``platform_service`` for machine-to-machine reads, ``platform_admin``
for human-driven mutations). RLS would gate every read/write on a
``current_setting('app.current_tenant_id')`` GUC that doesn't apply
here — the platform IS the trust boundary.

This module wraps ``get_service_pool`` (BYPASSRLS) in a FastAPI
``Depends``-compatible async generator so:

  * admin handlers consume one connection per request via
    ``Depends(get_admin_service_conn)``,
  * tests override the dep cleanly with
    ``app.dependency_overrides[get_admin_service_conn] = ...`` instead
    of mutating the pool singleton,
  * the lint allowlist at ``apps/api/scripts/lint_service_pool.py``
    catches any drift: only files matching ``services/admin/*`` may
    import ``get_service_pool``, so the surface stays narrow.

Why this lives under ``services/admin/`` rather than inline in the
router: the existing lint allowlist already permits the path, so this
placement piggybacks on Phase 1 conventions without adding a new
allowlist entry. Adding the router directly to the allowlist would
have been a `lint-comment-as-ADR` event (per ADR-055/057); the path-
based exception is the lower-noise option.

Phase 3 wired `routers/admin_ingestion.py` to ``get_anon_pool`` in PR-1,
which silently 404'd / 0-row'd every admin call in production because
``tenants`` + ``ingestion_requests`` both have self_* RLS policies
that depend on the GUC. The mocked tests passed, masking the bug.
PR-4 swaps to this service-pool helper so the endpoints actually work
against real Supabase.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg

from polaris_api.db.pools import get_service_pool


async def get_admin_service_conn() -> AsyncIterator[asyncpg.Connection]:
    """Yield one BYPASSRLS connection for an admin handler.

    No transaction is opened here — the handler decides whether to
    wrap its operations (audit writes always need a transaction to
    interlock with the action being audited). Acquiring the connection
    is enough; the handler may call ``async with conn.transaction():``
    as needed.

    The connection is released automatically when the async generator
    exits, including on handler exceptions.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        yield conn
