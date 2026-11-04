"""tenant_secrets persistence — Phase 1 PR-3.

Covers `services/secrets.py` put / get / rotate against a real Postgres + the
real PR-2 dual pool. The unit-level encrypt/decrypt round-trip is already
covered by `test_secrets.py`; this file exercises the DB-backed surface +
the cross-tenant RLS boundary.

Strategy
--------
Reuse the PR-2 integration plumbing from `test_tenant_context.py`:
- the session-scoped `integration_pool_bootstrap` fixture applies migrations,
  bootstraps role passwords, and opens both pools.
- per-test setup seeds two tenants via service_pool (BYPASSRLS).
- put/get/rotate calls use the anon_pool with `SET LOCAL app.current_tenant_id`
  so they hit RLS the same way TenantContext-fronted handlers will.

Cross-tenant invariant: tenant A cannot read tenant B's secret blob even
when both share the same `kind`. The decrypt step double-checks this: the
AAD is bound to `tenant_id`, so a leaked row would fail AEAD verification
with `CryptoError("decrypt failed")` even if RLS were somehow bypassed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

# Same JWT secret bootstrap as test_tenant_context.py so settings construct
# cleanly when this test module is imported in isolation. The value is
# unused here — we don't go through the FastAPI dep — but the polaris_api
# config layer is loaded transitively via the pools module.
os.environ.setdefault("JWT_SECRET", "pytest-secret-32-bytes-padded-xxxxxxxxxx")
os.environ.setdefault("JWT_ISSUER", "polaris-portal")

# Deterministic 32-byte test KMS key. Different from any real key.
_TEST_KMS_KEY_HEX = "00112233445566778899aabbccddeeff" "ffeeddccbbaa99887766554433221100"
os.environ.setdefault("POLARIS_KMS_KEY", _TEST_KMS_KEY_HEX)

from polaris_api.config import get_settings  # noqa: E402
from polaris_api.db import pools as pools_module  # noqa: E402
from polaris_api.services import secrets as secrets_module  # noqa: E402
from polaris_api.services.secrets import (  # noqa: E402
    CryptoError,
    decrypt,
    encrypt,
    get,
    put,
    rotate,
)


def _superuser_dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest_asyncio.fixture(scope="session")
async def persistence_bootstrap() -> AsyncIterator[tuple[str, str]]:
    """Apply migrations + bootstrap pool DSNs once per session.

    Lighter-touch sibling of `test_tenant_context.py::integration_pool_bootstrap`
    — duplicated rather than imported to keep test files independent."""
    dsn = _superuser_dsn()
    if not dsn:
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL")

    # Apply migrations via apply.py subprocess — matches `make migrate`.
    repo_root = Path(__file__).resolve().parents[3]  # noqa: ASYNC240 — session-setup one-shot
    apply_script = repo_root / "packages" / "db-migrations" / "apply.py"
    env = {**os.environ, "POLARIS_DATABASE_URL": dsn}
    proc = subprocess.run(  # noqa: S603, ASYNC221 — session-setup; equivalent to `make migrate`
        [sys.executable, str(apply_script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"migration apply failed: {proc.stderr or proc.stdout}")

    # Role passwords + per-role DSNs. If the pool bootstrap fixture from
    # test_tenant_context.py already ran, the passwords are set — ALTER ROLE
    # is idempotent so re-running is harmless.
    test_pw_anon = "anon_pytest_pw"
    test_pw_service = "svc_pytest_pw"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(  # noqa: S608 — constant password literals
            f"ALTER ROLE polaris_anon    WITH PASSWORD '{test_pw_anon}'"
        )
        await conn.execute(  # noqa: S608
            f"ALTER ROLE polaris_service WITH PASSWORD '{test_pw_service}'"
        )
    finally:
        await conn.close()

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

    os.environ["POLARIS_DB_URL_ANON"] = anon_dsn
    os.environ["POLARIS_DB_URL_SERVICE"] = service_dsn
    get_settings.cache_clear()

    # Pools may already be initialised by a sibling test module's fixture.
    # `init_pools` raises on double-init, so guard.
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


@pytest_asyncio.fixture
async def two_tenants(
    persistence_bootstrap: tuple[str, str],
) -> AsyncIterator[tuple[UUID, UUID]]:
    """Seed two active tenants via service_pool. Cleanup at teardown."""
    del persistence_bootstrap
    service_pool = pools_module.get_service_pool()
    # Use timestamp-based slug suffix so re-runs don't collide on UNIQUE(slug)
    # even if the previous cleanup missed.
    suffix_a = f"{time.time_ns()}-{uuid4().hex[:6]}"
    suffix_b = f"{time.time_ns()}-{uuid4().hex[:6]}"
    slug_a = f"pytest-sec-a-{suffix_a}"
    slug_b = f"pytest-sec-b-{suffix_b}"
    async with service_pool.acquire() as conn:
        a = await conn.fetchval(
            "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug_a
        )
        b = await conn.fetchval(
            "INSERT INTO tenants (slug) VALUES ($1) RETURNING id", slug_b
        )
    try:
        yield a, b
    finally:
        async with service_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM tenant_secrets WHERE tenant_id IN ($1, $2)", a, b
            )
            await conn.execute("DELETE FROM tenants WHERE id IN ($1, $2)", a, b)


async def _tenant_conn(tenant_id: UUID) -> asyncpg.Connection:
    """Acquire an anon-pool connection inside an open transaction + SET LOCAL
    app.current_tenant_id. Caller MUST close+rollback explicitly via the
    returned connection's transaction context.

    For tests we use a manual acquire/release pattern rather than
    `pool.acquire()` async-context-manager because we want to inspect the
    GUC via the same connection across multiple awaits in the test body
    without nested `async with`s.
    """
    pool = pools_module.get_anon_pool()
    conn = await pool.acquire()
    return conn


# ----------------------------------------------------------------------------
# Wire-format helpers — unit-test the split/join logic without a DB
# ----------------------------------------------------------------------------


def test_split_then_join_is_identity() -> None:
    """`_join_wire(_split_wire(w))` must reconstruct the original wire blob."""
    tenant = uuid4()
    wire = encrypt(b"some-credential", tenant)
    nonce, ct = secrets_module._split_wire(wire)  # noqa: SLF001 — internal helper under test
    rejoined = secrets_module._join_wire(nonce, ct)  # noqa: SLF001
    assert rejoined == wire
    # And decrypt still works on the rejoined form.
    assert decrypt(rejoined, tenant) == b"some-credential"


def test_split_rejects_short_blob() -> None:
    with pytest.raises(CryptoError, match="too short"):
        secrets_module._split_wire(b"\x01" + b"x" * 5)  # noqa: SLF001


def test_split_rejects_unknown_version() -> None:
    tenant = uuid4()
    wire = encrypt(b"x", tenant)
    bad = b"\x99" + wire[1:]
    with pytest.raises(CryptoError, match="unsupported"):
        secrets_module._split_wire(bad)  # noqa: SLF001


# ----------------------------------------------------------------------------
# put / get / rotate — round-trip integration tests
# ----------------------------------------------------------------------------


async def test_put_then_get_round_trip(two_tenants: tuple[UUID, UUID]) -> None:
    tenant_a, _ = two_tenants
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        await put(conn, tenant_a, "wa_token", b"super-secret-token-value")
        recovered = await get(conn, tenant_a, "wa_token")
        assert recovered == b"super-secret-token-value"


async def test_get_returns_none_for_missing_kind(
    two_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, _ = two_tenants
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        assert await get(conn, tenant_a, "never-written") is None


async def test_put_is_upsert_and_updates_rotated_at(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """Second put() for same (tenant_id, kind) replaces in place + sets rotated_at."""
    tenant_a, _ = two_tenants
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        await put(conn, tenant_a, "ea_key", b"v1-value")
        rotated_at_v1 = await conn.fetchval(
            "SELECT rotated_at FROM tenant_secrets WHERE tenant_id = $1 AND kind = $2",
            tenant_a,
            "ea_key",
        )
        assert rotated_at_v1 is None  # first insert leaves rotated_at NULL

        await put(conn, tenant_a, "ea_key", b"v2-value")
        rotated_at_v2 = await conn.fetchval(
            "SELECT rotated_at FROM tenant_secrets WHERE tenant_id = $1 AND kind = $2",
            tenant_a,
            "ea_key",
        )
        assert rotated_at_v2 is not None

        # And the value is the new one.
        assert await get(conn, tenant_a, "ea_key") == b"v2-value"


async def test_rotate_changes_nonce_and_sets_rotated_at(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """rotate() re-encrypts with a fresh nonce — same plaintext but different
    ciphertext bytes — and updates rotated_at."""
    tenant_a, _ = two_tenants
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        await put(conn, tenant_a, "anthropic_key", b"sk-ant-abc123")
        before = await conn.fetchrow(
            "SELECT nonce, ciphertext FROM tenant_secrets WHERE tenant_id = $1 AND kind = $2",
            tenant_a,
            "anthropic_key",
        )

        await rotate(conn, tenant_a, "anthropic_key")
        after = await conn.fetchrow(
            "SELECT nonce, ciphertext, rotated_at FROM tenant_secrets WHERE tenant_id = $1 AND kind = $2",
            tenant_a,
            "anthropic_key",
        )

        assert before is not None and after is not None
        assert bytes(after["nonce"]) != bytes(before["nonce"]), "nonce must change on rotate"
        assert bytes(after["ciphertext"]) != bytes(before["ciphertext"])
        assert after["rotated_at"] is not None
        # Plaintext recovers unchanged.
        assert await get(conn, tenant_a, "anthropic_key") == b"sk-ant-abc123"


async def test_rotate_missing_secret_raises(two_tenants: tuple[UUID, UUID]) -> None:
    tenant_a, _ = two_tenants
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        with pytest.raises(CryptoError, match="cannot rotate: secret missing"):
            await rotate(conn, tenant_a, "missing-kind")


async def test_cross_tenant_get_returns_none_via_rls(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """Tenant A writes a secret; tenant B's session must see nothing.

    Both tenants use the same `kind` to prove RLS is the gate (not a
    coincidental kind mismatch).
    """
    tenant_a, tenant_b = two_tenants
    pool = pools_module.get_anon_pool()

    # Tenant A writes.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        await put(conn, tenant_a, "shared_kind", b"A-secret")

    # Tenant B reads. RLS makes the row invisible → returns None.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_b)
        )
        assert await get(conn, tenant_b, "shared_kind") is None


async def test_cross_tenant_put_under_wrong_tenant_id_blocked(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """When session belongs to A, an INSERT carrying tenant_id=B trips the
    RLS WITH CHECK clause."""
    tenant_a, tenant_b = two_tenants
    pool = pools_module.get_anon_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_a)
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            # Use put() directly with the wrong tenant_id — encrypt will
            # bind AAD to B, INSERT will fail RLS.
            await put(conn, tenant_b, "shared_kind", b"attempted-A-write")
