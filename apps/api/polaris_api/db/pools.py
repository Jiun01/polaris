"""Dual asyncpg connection pools — Phase 1 PR-2.

Two pools per ADR-002:

- `anon_pool` — connects as `polaris_anon` (NOBYPASSRLS). Every request-scoped
  query path uses this pool through `TenantContext`, which opens a transaction
  and `SELECT set_config('app.current_tenant_id', $1, true)` before running
  the query. RLS at Postgres is the structural defence — even if an app bug
  forgets to filter by tenant_id, the policy gates the read.

- `service_pool` — connects as `polaris_service` (BYPASSRLS). Reserved for
  cron, admin (`/api/admin/*`), and the migration applier. A CI lint
  (`scripts/lint_service_pool.py`) blocks imports of `get_service_pool`
  outside the allowlist (`services/admin/`, `jobs/`, `packages/db-migrations/`).

Both pools are lifespan-managed singletons: `init_pools()` opens them at
startup, `close_pools()` drains them at shutdown. Calling `get_*_pool()`
before `init_pools()` raises a clear error rather than lazy-initialising —
lazy init makes pool startup costs invisible and breaks the "DSN required"
contract.
"""

from __future__ import annotations

import json

import asyncpg

from polaris_api.config import get_settings


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register JSON/JSONB codecs so asyncpg returns parsed dicts, not strings."""
    for pg_type in ("json", "jsonb"):
        await conn.set_type_codec(
            pg_type,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


def _ssl_for_dsn(dsn: str) -> str | None:
    """Return asyncpg's ``ssl='require'`` string for hosts that need TLS.

    Passing the string ``'require'`` tells asyncpg to upgrade to SSL without
    certificate chain verification — the same semantics as libpq's
    ``sslmode=require``. An ``ssl.SSLContext`` object routes through a
    different asyncpg code path that times out against Supabase's pooler on
    Windows; the string form is confirmed-working (manual asyncpg.connect test).

    Returns ``None`` for plain-TCP targets (local Docker) so dev keeps working.
    """
    if "supabase.com" in dsn or "sslmode=require" in dsn:
        return "require"
    return None

_anon_pool: asyncpg.Pool | None = None
_service_pool: asyncpg.Pool | None = None

# Pool sizing — start conservative, raise after we measure. Supabase's pooler
# limits free tier ≈60 connections per project, so anon (10) + service (6)
# leaves headroom for portal + workers + ad-hoc psql sessions.
#
# audit Batch B (B8, LAST task in the batch): service pool raised 4 -> 6 —
# ONLY after B7 (scheduled/*.py conn-scoping: ea_settings_sync, retention_
# sweep, cost_ceiling_check, quota_flush, invoice_overdue) cut every
# scheduled job's conn-hold time (no job now holds a conn across an EA/Mimir
# HTTP call, or across N-tenant loops). Raising the ceiling first would have
# masked those hold-time bugs instead of fixing them, and eaten pooler
# headroom shared with portal/workers. 8 cron jobs + admin + 3 in-process
# workers all draw from this one pool; 6 gives the nightly-window jobs
# (quota_flush 00:05, retention_sweep 02:00, cost_ceiling_check 03:00,
# audit_chain_verify 04:00) more headroom without starving the hourly
# ea_settings_sync tick or admin API traffic.
_ANON_MIN_SIZE = 2
_ANON_MAX_SIZE = 10
_SERVICE_MIN_SIZE = 1
_SERVICE_MAX_SIZE = 6


async def init_pools() -> None:
    """Open both pools. Called from `main.py` lifespan startup.

    Raises if either DSN env var is unset — refusing to boot is the right
    behaviour: a half-pooled API would surface as cryptic per-request errors
    instead of one loud boot failure.
    """
    global _anon_pool, _service_pool  # noqa: PLW0603 — module-level singletons by design
    if _anon_pool is not None or _service_pool is not None:
        raise RuntimeError("init_pools called twice — lifespan double-start?")

    settings = get_settings()
    if not settings.polaris_db_url_anon:
        raise RuntimeError(
            "POLARIS_DB_URL_ANON is unset. Apply migration 010 and set the "
            "polaris_anon role password; see ../docs/internal-ops/RUNBOOK.md 'PR-2 role bootstrap'."
        )
    if not settings.polaris_db_url_service:
        raise RuntimeError(
            "POLARIS_DB_URL_SERVICE is unset. Apply migration 010 and set the "
            "polaris_service role password; see ../docs/internal-ops/RUNBOOK.md 'PR-2 role bootstrap'."
        )

    # NOTE: the request-path hang is bounded where it matters — ``pool.acquire``
    # in deps/tenant.py takes an explicit timeout that maps to a retryable 503
    # (QA finding M5). We do NOT pass a connection-establishment timeout to
    # ``create_pool`` here: asyncpg has no ``setup_timeout`` kwarg (it forwards
    # unknown kwargs to ``connect()``, which rejected it and crashed boot), and
    # a blanket ``command_timeout`` would false-positive on the long batch jobs
    # the service pool runs (cost/quota flush).
    _anon_pool = await asyncpg.create_pool(
        dsn=settings.polaris_db_url_anon,
        min_size=_ANON_MIN_SIZE,
        max_size=_ANON_MAX_SIZE,
        ssl=_ssl_for_dsn(settings.polaris_db_url_anon),
        init=_init_conn,
    )
    _service_pool = await asyncpg.create_pool(
        dsn=settings.polaris_db_url_service,
        min_size=_SERVICE_MIN_SIZE,
        max_size=_SERVICE_MAX_SIZE,
        ssl=_ssl_for_dsn(settings.polaris_db_url_service),
        init=_init_conn,
    )


async def close_pools() -> None:
    """Drain both pools. Called from `main.py` lifespan shutdown."""
    global _anon_pool, _service_pool  # noqa: PLW0603
    if _anon_pool is not None:
        await _anon_pool.close()
        _anon_pool = None
    if _service_pool is not None:
        await _service_pool.close()
        _service_pool = None


def get_anon_pool() -> asyncpg.Pool:
    """Return the anon pool. Raises if `init_pools()` hasn't been called.

    Synchronous (no `async`) because the pool is already constructed; there's
    nothing to await. FastAPI deps that need the pool call this from within an
    async function.
    """
    if _anon_pool is None:
        raise RuntimeError("anon pool not initialised — call init_pools() first")
    return _anon_pool


def get_service_pool() -> asyncpg.Pool:
    """Return the BYPASSRLS service pool.

    CI lint forbids importing this outside `services/admin/`, `jobs/`, and
    `packages/db-migrations/`. The grep gate runs in pytest
    (`apps/api/tests/test_pools_lint.py`) so a misuse fails CI loudly.
    """
    if _service_pool is None:
        raise RuntimeError("service pool not initialised — call init_pools() first")
    return _service_pool


async def reset_service_pool() -> None:
    """Close and reopen the service pool after a Supavisor auth-cache eviction.

    Supabase's Supavisor pooler periodically evicts cached custom-role credentials
    across its nodes. When that happens, every ``pool.acquire()`` raises
    ``InvalidPasswordError`` even though the DSN password itself is correct.
    Closing and recreating the pool forces asyncpg to open fresh connections,
    which re-negotiate credentials with whichever Supavisor node answers next.
    """
    global _service_pool  # noqa: PLW0603
    if _service_pool is not None:
        try:
            await _service_pool.close()
        except Exception:  # noqa: BLE001, S110
            pass
        _service_pool = None
    settings = get_settings()
    _service_pool = await asyncpg.create_pool(
        dsn=settings.polaris_db_url_service,
        min_size=_SERVICE_MIN_SIZE,
        max_size=_SERVICE_MAX_SIZE,
        ssl=_ssl_for_dsn(settings.polaris_db_url_service),
        init=_init_conn,
    )
