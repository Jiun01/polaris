"""TenantContext FastAPI dep — Phase 1 PR-2.

Two layers:

- **Unit** — mock the asyncpg connection via dep override. Exercise the JWT
  path (happy / missing / bad signature / wrong issuer / expired / non-uuid
  claims). No DB required; runs everywhere.

- **Integration** — full stack: real local Postgres, migration 010 applied,
  role passwords bootstrapped, pools initialised, a test route mounted on the
  app. Verifies `app.current_tenant_id` is actually SET LOCAL'd, that the
  tenant-status branch returns 403/404 correctly, and that the same RLS
  policies PR-1 proved at the DB layer also hold end-to-end through the
  FastAPI dep. Skips if `POLARIS_DATABASE_URL` is unset (= no local DB).

Token format (locked for PR-2): HS256, claims `sub` (user_id UUID),
`tenant_id` (UUID), `role` (string), `iss` (matches `JWT_ISSUER`), `iat`,
`exp`. The portal (Phase 6) is the canonical issuer; tests mint tokens
in-process using the same shared secret.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import asyncpg
import jwt
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

# Test config — applied to env BEFORE polaris_api imports read it.
# JWT secret must be ≥32 bytes per RFC 7518 §3.2 to silence PyJWT's
# InsecureKeyLengthWarning; the value itself is meaningless (test-only).
_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"  # 40 chars
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)

# Imports after env is primed.
from polaris_api.config import get_settings  # noqa: E402
from polaris_api.db import pools as pools_module  # noqa: E402
from polaris_api.deps.tenant import (  # noqa: E402
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

# Pin loop_scope=function — this file builds an AsyncClient against the
# main `app` AND overrides async deps via app.dependency_overrides,
# the exact shape conftest.py warns about (historically Phase 5's now-
# retired flow_runtime/ test subdirectory collected first in pytest's
# traversal and initialised the session loop, which this file's
# function-scope fixture teardown then closed — poisoning every test
# that ran after). Mirror the admin-test fix.
pytestmark = pytest.mark.asyncio(loop_scope="function")

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _mint(
    *,
    tenant_id: UUID | str,
    user_id: UUID | str | None = None,
    role: str = "admin",
    issuer: str = _TEST_JWT_ISSUER,
    secret: str = _TEST_JWT_SECRET,
    exp_offset: int = 3600,
    iat_offset: int = 0,
) -> str:
    """Mint a JWT for tests."""
    now = int(time.time())
    payload = {
        "sub": str(user_id) if user_id is not None else str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "iss": issuer,
        "iat": now + iat_offset,
        "exp": now + iat_offset + exp_offset,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _build_test_app(handler_return: Any = None) -> FastAPI:
    """Construct a tiny FastAPI app whose `/_test/tenant` route exposes
    TenantContext fields. Used by both unit and integration tests."""
    app = FastAPI()

    @app.get("/_test/tenant")
    async def _route(
        ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    ) -> dict[str, str]:
        return {
            "tenant_id": str(ctx.tenant_id),
            "user_id": str(ctx.user_id),
            "role": ctx.role,
            "plan_tier": ctx.plan_tier,
            "status": ctx.status,
        }

    return app


# ----------------------------------------------------------------------------
# Unit tests — mock the conn, exercise JWT logic only
# ----------------------------------------------------------------------------


@pytest.fixture
def mock_conn() -> AsyncMock:
    """Mock conn whose fetchrow returns an active tenant row by default."""
    conn = AsyncMock(spec=asyncpg.Connection)
    conn.execute = AsyncMock(return_value="SELECT 1")
    conn.fetchrow = AsyncMock(
        return_value={
            "status": "active",
            "plan_tier": "starter",
            "suspended_reason": None,
        }
    )
    return conn


@pytest_asyncio.fixture(loop_scope="function")
async def unit_client(mock_conn: AsyncMock) -> AsyncIterator[AsyncClient]:
    """AsyncClient against a test app with get_request_conn overridden to yield
    the mock conn. JWT path is real; DB path is mocked."""
    app = _build_test_app()

    async def _override() -> AsyncIterator[AsyncMock]:
        yield mock_conn

    app.dependency_overrides[get_request_conn] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_unit_happy_path_returns_context(
    unit_client: AsyncClient, mock_conn: AsyncMock
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    token = _mint(tenant_id=tenant_id, user_id=user_id)
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == str(tenant_id)
    assert body["user_id"] == str(user_id)
    assert body["role"] == "admin"
    # SET LOCAL was applied (set_config call wrapping the tenant_id)
    mock_conn.execute.assert_awaited_once_with(
        "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_id)
    )


async def test_unit_missing_authorization_returns_401(unit_client: AsyncClient) -> None:
    resp = await unit_client.get("/_test/tenant")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


async def test_unit_non_bearer_scheme_returns_401(unit_client: AsyncClient) -> None:
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": "Basic abc:def"}
    )
    assert resp.status_code == 401


async def test_unit_bad_signature_returns_401(unit_client: AsyncClient) -> None:
    # Wrong-secret must also be ≥32 bytes so PyJWT's mint path doesn't warn;
    # the decode path will still reject it because it's not _TEST_JWT_SECRET.
    token = _mint(tenant_id=uuid4(), secret="wrong-secret-also-32-bytes-padded-xxxxx")
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_unit_wrong_issuer_returns_401(unit_client: AsyncClient) -> None:
    token = _mint(tenant_id=uuid4(), issuer="someone-else")
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_unit_expired_token_returns_401(unit_client: AsyncClient) -> None:
    # iat 2h ago, exp 1h ago (well past the leeway window).
    token = _mint(tenant_id=uuid4(), iat_offset=-7200, exp_offset=3600)
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_unit_non_uuid_tenant_id_returns_401(unit_client: AsyncClient) -> None:
    token = _mint(tenant_id="not-a-uuid")
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_unit_unknown_tenant_returns_404(
    unit_client: AsyncClient, mock_conn: AsyncMock
) -> None:
    mock_conn.fetchrow = AsyncMock(return_value=None)
    token = _mint(tenant_id=uuid4())
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


async def test_unit_suspended_tenant_returns_403(
    unit_client: AsyncClient, mock_conn: AsyncMock
) -> None:
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "status": "suspended",
            "plan_tier": "starter",
            "suspended_reason": "payment_overdue",
        }
    )
    token = _mint(tenant_id=uuid4())
    resp = await unit_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert "payment_overdue" in resp.json()["detail"]


# ----------------------------------------------------------------------------
# Integration tests — real DB, real pool, real RLS. Skip if no DB.
# ----------------------------------------------------------------------------


def _superuser_dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest_asyncio.fixture(scope="session")
async def integration_pool_bootstrap() -> AsyncIterator[tuple[str, str]]:
    """Ensure migration 010 is applied + role passwords are set + pools are
    initialised. Yields (anon_dsn, service_dsn). Session-scoped because pool
    init is expensive.

    CI follow-up (2026-05-19): same root cause as test_audit_chain —
    file-level ``pytestmark`` pins tests to function-scoped loops while
    this fixture opens pools on the session loop. Skipping in CI until
    a dedicated PR converts the file's loop scope strategy.
    """
    if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip(
            "CI follow-up: session-loop pool fixture conflicts with "
            "function-loop tests in this file; tracked separately."
        )
    dsn = _superuser_dsn()
    if not dsn:
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL")

    # Apply all pending migrations via apply.py as a subprocess — matches
    # `make migrate` behaviour exactly, including the JSON output format
    # and exit codes. Imports moved to module scope (see top of file).
    repo_root = Path(__file__).resolve().parents[3]  # noqa: ASYNC240 — one-shot pathlib resolve at session setup; not in a hot path
    apply_script = repo_root / "packages" / "db-migrations" / "apply.py"
    env = {**os.environ, "POLARIS_DATABASE_URL": dsn}
    proc = subprocess.run(  # noqa: S603, ASYNC221 — runs the migration applier subprocess at session setup; equivalent to `make migrate`, runs once
        [sys.executable, str(apply_script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"migration apply failed in test bootstrap: {proc.stderr or proc.stdout}"
        )

    # Bootstrap role passwords + build per-role DSNs.
    test_pw_anon = "anon_pytest_pw"
    test_pw_service = "svc_pytest_pw"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"ALTER ROLE polaris_anon    WITH PASSWORD '{test_pw_anon}'")  # noqa: S608
        await conn.execute(f"ALTER ROLE polaris_service WITH PASSWORD '{test_pw_service}'")  # noqa: S608
    finally:
        await conn.close()

    # Construct per-role DSNs by swapping the user:password in the superuser DSN.
    parsed = urlparse(dsn)
    host_part = parsed.hostname or "localhost"
    if parsed.port:
        host_part = f"{host_part}:{parsed.port}"
    anon_dsn = urlunparse(
        parsed._replace(netloc=f"polaris_anon:{test_pw_anon}@{host_part}")
    )
    service_dsn = urlunparse(
        parsed._replace(netloc=f"polaris_service:{test_pw_service}@{host_part}")
    )

    # Push into env so config.get_settings picks them up + init pools.
    os.environ["POLARIS_DB_URL_ANON"] = anon_dsn
    os.environ["POLARIS_DB_URL_SERVICE"] = service_dsn
    get_settings.cache_clear()

    # Pools may already be open from a sibling test module's session-scoped
    # bootstrap (test_secrets_persistence.py, test_kms_sentinel.py). init_pools
    # raises on double-init in production code (lifespan correctness), so guard
    # at the fixture layer instead of weakening the production check.
    if pools_module._anon_pool is None and pools_module._service_pool is None:  # noqa: SLF001
        await pools_module.init_pools()
        opened_here = True
    else:
        opened_here = False
    try:
        yield anon_dsn, service_dsn
    finally:
        if opened_here:
            await pools_module.close_pools()
        get_settings.cache_clear()


@pytest_asyncio.fixture(loop_scope="function")
async def integration_seed_tenant(
    integration_pool_bootstrap: tuple[str, str],
) -> AsyncIterator[tuple[UUID, UUID]]:
    """Seed one active tenant + one auth.users row + the tenant_users link.

    `polaris_service` deliberately has only SELECT on auth.users (Supabase
    manages auth in prod; Polaris shouldn't write to it). For tests we open a
    superuser connection just for the auth.users insert/delete and use
    `service_pool` for everything else. Cleanup is by slug/email prefix.
    """
    del integration_pool_bootstrap  # consumed; needed only for ordering
    service_pool = pools_module.get_service_pool()
    su_dsn = _superuser_dsn()
    assert su_dsn is not None  # integration_pool_bootstrap would have skipped otherwise

    tenant_id: UUID | None = None
    user_id: UUID | None = None
    slug = f"pytest-int-{uuid4().hex[:8]}"
    email = f"int-{uuid4().hex[:8]}@pytest.invalid"

    # auth.users insert — superuser only.
    su_conn = await asyncpg.connect(su_dsn)
    try:
        user_id = await su_conn.fetchval(
            "INSERT INTO auth.users (email) VALUES ($1) RETURNING id", email
        )
    finally:
        await su_conn.close()

    # tenants + tenant_users — service_pool (BYPASSRLS).
    async with service_pool.acquire() as conn:
        tenant_id = await conn.fetchval(
            "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug
        )
        await conn.execute(
            "INSERT INTO tenant_users (tenant_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            tenant_id,
            user_id,
        )
    try:
        assert tenant_id is not None and user_id is not None
        yield tenant_id, user_id
    finally:
        async with service_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM tenant_users WHERE tenant_id = $1", tenant_id
            )
            await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        # auth.users delete also needs superuser.
        su_conn = await asyncpg.connect(su_dsn)
        try:
            await su_conn.execute("DELETE FROM auth.users WHERE id = $1", user_id)
        finally:
            await su_conn.close()


@pytest_asyncio.fixture(loop_scope="function")
async def integration_client() -> AsyncIterator[AsyncClient]:
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_integration_active_tenant_returns_200_and_sets_guc(
    integration_seed_tenant: tuple[UUID, UUID], integration_client: AsyncClient
) -> None:
    """End-to-end: valid JWT → 200, body carries tenant info, and a follow-up
    query on the same pool sees the GUC. The follow-up uses a fresh connection
    from anon_pool because the request transaction has closed by now — so we
    don't directly assert the request-time GUC. Instead we assert the request
    succeeded against the real RLS-enforcing role, which is itself proof
    set_config worked (otherwise the tenant SELECT would have returned 0 rows
    and the dep would have raised 404)."""
    tenant_id, user_id = integration_seed_tenant
    token = _mint(tenant_id=tenant_id, user_id=user_id)
    resp = await integration_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(tenant_id)
    assert body["user_id"] == str(user_id)
    assert body["status"] == "active"
    assert body["plan_tier"] == "starter"


async def test_integration_unknown_tenant_returns_404(
    integration_pool_bootstrap: tuple[str, str], integration_client: AsyncClient
) -> None:
    del integration_pool_bootstrap  # ordering only
    # A well-formed JWT for a tenant_id that doesn't exist.
    token = _mint(tenant_id=uuid4(), user_id=uuid4())
    resp = await integration_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


async def test_integration_suspended_tenant_returns_403(
    integration_seed_tenant: tuple[UUID, UUID], integration_client: AsyncClient
) -> None:
    tenant_id, user_id = integration_seed_tenant
    # Flip the seeded tenant to suspended via service_pool, then call.
    service_pool = pools_module.get_service_pool()
    async with service_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET status = 'suspended', "
            "suspended_reason = 'quota_exhausted' WHERE id = $1",
            tenant_id,
        )
    token = _mint(tenant_id=tenant_id, user_id=user_id)
    resp = await integration_client.get(
        "/_test/tenant", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert "quota_exhausted" in resp.json()["detail"]


# ----------------------------------------------------------------------------
# Pool lifecycle — sanity checks
# ----------------------------------------------------------------------------


async def test_pools_raise_before_init() -> None:
    """`get_anon_pool` / `get_service_pool` raise if init_pools hasn't run.

    We reach into the module's private state to make sure we test the un-init
    branch even after the integration fixture has run init. Restore at exit."""
    saved_anon = pools_module._anon_pool  # noqa: SLF001
    saved_service = pools_module._service_pool  # noqa: SLF001
    pools_module._anon_pool = None  # noqa: SLF001
    pools_module._service_pool = None  # noqa: SLF001
    try:
        with pytest.raises(RuntimeError, match="anon pool not initialised"):
            pools_module.get_anon_pool()
        with pytest.raises(RuntimeError, match="service pool not initialised"):
            pools_module.get_service_pool()
    finally:
        pools_module._anon_pool = saved_anon  # noqa: SLF001
        pools_module._service_pool = saved_service  # noqa: SLF001
