"""KMS sentinel verify + seed — Phase 1 PR-3.

Three layers:

- **Unit (pure crypto)**: `encrypt_platform_sentinel` + `decrypt_platform_sentinel`
  round-trip; AAD distinct from the tenant-scoped path; tampered ciphertext
  raises.

- **Integration (DB + service_pool)**: `seed_sentinel()` writes a single
  platform_health row; `verify_sentinel()` returns True for the seeded key
  and raises CryptoError if the key was swapped between seed and verify.
  Tests use the same session bootstrap as `test_secrets_persistence.py`.

- **Boot semantics**: the lifespan helper `_verify_kms_sentinel` is the
  consumer; we don't re-test it here because end-to-end boot tests would
  rebuild the entire ASGI app per case. The behaviour is exercised
  indirectly through the integration tests below + by a small lifespan
  smoke test in test_tenant_context.py's integration plumbing.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio

os.environ.setdefault("JWT_SECRET", "pytest-secret-32-bytes-padded-xxxxxxxxxx")
os.environ.setdefault("JWT_ISSUER", "polaris-portal")

# Two distinct 32-byte test KMS keys. _KEY_A is the one we seed under;
# _KEY_B simulates an operator rotation without re-seeding.
_KEY_A_HEX = "00112233445566778899aabbccddeeff" "ffeeddccbbaa99887766554433221100"
_KEY_B_HEX = "deadbeefdeadbeefdeadbeefdeadbeef" "deadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("POLARIS_KMS_KEY", _KEY_A_HEX)

from polaris_api.config import get_settings  # noqa: E402
from polaris_api.db import pools as pools_module  # noqa: E402
from polaris_api.services import secrets as secrets_module  # noqa: E402
from polaris_api.services.kms_sentinel import (  # noqa: E402
    SENTINEL_PLAINTEXT,
    seed_sentinel,
    verify_sentinel,
)
from polaris_api.services.secrets import (  # noqa: E402
    PLATFORM_SENTINEL_AAD,
    CryptoError,
    decrypt_platform_sentinel,
    encrypt_platform_sentinel,
)

# ----------------------------------------------------------------------------
# Unit — platform sentinel encrypt/decrypt
# ----------------------------------------------------------------------------


def _ensure_key_a() -> None:
    os.environ["POLARIS_KMS_KEY"] = _KEY_A_HEX
    secrets_module._get_aead.cache_clear()  # noqa: SLF001


def _ensure_key_b() -> None:
    os.environ["POLARIS_KMS_KEY"] = _KEY_B_HEX
    secrets_module._get_aead.cache_clear()  # noqa: SLF001


def test_platform_sentinel_round_trip() -> None:
    _ensure_key_a()
    ct = encrypt_platform_sentinel(SENTINEL_PLAINTEXT)
    assert decrypt_platform_sentinel(ct) == SENTINEL_PLAINTEXT


def test_platform_sentinel_aad_is_distinct_from_tenant_aad() -> None:
    """A sentinel ciphertext must not decrypt via the tenant-scoped path
    (and vice versa). Guards against API confusion that would let a tenant
    secret masquerade as a sentinel or vice versa."""
    _ensure_key_a()
    sentinel_ct = encrypt_platform_sentinel(SENTINEL_PLAINTEXT)
    from uuid import UUID  # local import to avoid top-level pollution

    # Try to decrypt the sentinel via the tenant path — must fail because
    # AAD differs.
    with pytest.raises(CryptoError, match="decrypt failed"):
        secrets_module.decrypt(sentinel_ct, UUID(int=0))


def test_platform_sentinel_tampered_ciphertext_raises() -> None:
    _ensure_key_a()
    ct = bytearray(encrypt_platform_sentinel(SENTINEL_PLAINTEXT))
    # Flip a byte in the ciphertext tail (tag region).
    ct[-1] ^= 0x01
    with pytest.raises(CryptoError, match="decrypt failed"):
        decrypt_platform_sentinel(bytes(ct))


def test_platform_sentinel_aad_constant_is_stable() -> None:
    """If anyone changes PLATFORM_SENTINEL_AAD, every existing sentinel row
    in every environment becomes undecryptable. This test pins the bytes."""
    assert PLATFORM_SENTINEL_AAD == b"polaris.platform.kms_sentinel:v1"


# ----------------------------------------------------------------------------
# Defensive input guards on the platform-sentinel surface
# ----------------------------------------------------------------------------


def test_encrypt_platform_sentinel_rejects_non_bytes() -> None:
    _ensure_key_a()
    with pytest.raises(CryptoError, match="plaintext must be bytes"):
        encrypt_platform_sentinel("not-bytes")  # type: ignore[arg-type]


def test_decrypt_platform_sentinel_rejects_non_bytes() -> None:
    _ensure_key_a()
    with pytest.raises(CryptoError, match="ciphertext must be bytes"):
        decrypt_platform_sentinel("not-bytes")  # type: ignore[arg-type]


def test_decrypt_platform_sentinel_rejects_short_ciphertext() -> None:
    _ensure_key_a()
    with pytest.raises(CryptoError, match="too short"):
        decrypt_platform_sentinel(b"\x01" + b"x" * 5)


def test_decrypt_platform_sentinel_rejects_unknown_version() -> None:
    _ensure_key_a()
    ct = encrypt_platform_sentinel(SENTINEL_PLAINTEXT)
    bad = b"\x99" + ct[1:]
    with pytest.raises(CryptoError, match="unsupported"):
        decrypt_platform_sentinel(bad)


def test_decrypt_tenant_rejects_non_bytes() -> None:
    """Mirror coverage for the tenant-scoped decrypt() guard."""
    _ensure_key_a()
    from uuid import UUID

    with pytest.raises(CryptoError, match="ciphertext must be bytes"):
        secrets_module.decrypt("not-bytes", UUID(int=0))  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# Integration — seed + verify against real Postgres
# ----------------------------------------------------------------------------


def _superuser_dsn() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


@pytest_asyncio.fixture(scope="session")
async def sentinel_bootstrap() -> AsyncIterator[None]:
    """Apply migrations + open the service pool. Mirrors
    `test_secrets_persistence.py::persistence_bootstrap` (duplicated for
    test-file independence)."""
    dsn = _superuser_dsn()
    if not dsn:
        pytest.skip("set POLARIS_DATABASE_URL or SUPABASE_DB_URL")

    repo_root = Path(__file__).resolve().parents[3]  # noqa: ASYNC240
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

    test_pw_anon = "anon_pytest_pw"
    test_pw_service = "svc_pytest_pw"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(  # noqa: S608
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

    if pools_module._anon_pool is None and pools_module._service_pool is None:  # noqa: SLF001
        await pools_module.init_pools()
        opened_here = True
    else:
        opened_here = False

    try:
        yield
    finally:
        if opened_here:
            await pools_module.close_pools()
        get_settings.cache_clear()


async def _clear_sentinel_row() -> None:
    """Delete the singleton platform_health row so each test starts clean.

    platform_health is keyed on `id = 1` so DELETE is the cleanest reset.
    """
    async with pools_module.get_service_pool().acquire() as conn:
        await conn.execute("DELETE FROM platform_health")


async def test_verify_returns_false_when_no_sentinel_row(
    sentinel_bootstrap: None,
) -> None:
    del sentinel_bootstrap
    await _clear_sentinel_row()
    async with pools_module.get_service_pool().acquire() as conn:
        seeded = await verify_sentinel(conn)
    assert seeded is False


async def test_seed_then_verify_returns_true(
    sentinel_bootstrap: None,
) -> None:
    del sentinel_bootstrap
    _ensure_key_a()
    await _clear_sentinel_row()
    async with pools_module.get_service_pool().acquire() as conn:
        await seed_sentinel(conn)
        assert await verify_sentinel(conn) is True


async def test_seed_writes_expected_hash(sentinel_bootstrap: None) -> None:
    """The persisted hash must equal SHA-256(SENTINEL_PLAINTEXT). If the
    constant ever drifts, this test catches it; if the hash computation
    drifts, this test catches that too."""
    del sentinel_bootstrap
    _ensure_key_a()
    await _clear_sentinel_row()
    async with pools_module.get_service_pool().acquire() as conn:
        await seed_sentinel(conn)
        row = await conn.fetchrow(
            "SELECT kms_sentinel_plaintext_hash FROM platform_health WHERE id = 1"
        )
        assert row is not None
        expected = hashlib.sha256(SENTINEL_PLAINTEXT).digest()
        assert bytes(row["kms_sentinel_plaintext_hash"]) == expected


async def test_verify_raises_on_key_swap(sentinel_bootstrap: None) -> None:
    """Seed under KEY_A, then swap to KEY_B and re-verify — must raise.

    This is the scenario the sentinel exists to catch: operator changed
    POLARIS_KMS_KEY without re-seeding (or without re-encrypting all
    tenant_secrets rows under the new key).
    """
    del sentinel_bootstrap
    _ensure_key_a()
    await _clear_sentinel_row()
    async with pools_module.get_service_pool().acquire() as conn:
        await seed_sentinel(conn)

    _ensure_key_b()
    try:
        async with pools_module.get_service_pool().acquire() as conn:
            with pytest.raises(CryptoError, match="kms sentinel|decrypt failed"):
                await verify_sentinel(conn)
    finally:
        _ensure_key_a()  # restore for downstream tests


async def test_reseed_replaces_existing_row(sentinel_bootstrap: None) -> None:
    """seed_sentinel is idempotent — calling it twice overwrites the row.

    The intentional key-rotation path is: change POLARIS_KMS_KEY → re-run
    the seed CLI → ciphertext is now under the new key. Test that pattern."""
    del sentinel_bootstrap
    _ensure_key_a()
    await _clear_sentinel_row()
    async with pools_module.get_service_pool().acquire() as conn:
        await seed_sentinel(conn)
        ct1 = await conn.fetchval(
            "SELECT kms_sentinel_ciphertext FROM platform_health WHERE id = 1"
        )
        # Re-seed (under the same key — ciphertext changes because nonce is fresh).
        await seed_sentinel(conn)
        ct2 = await conn.fetchval(
            "SELECT kms_sentinel_ciphertext FROM platform_health WHERE id = 1"
        )
        assert bytes(ct1) != bytes(ct2), "reseed must produce a fresh nonce"
        # And verify still passes.
        assert await verify_sentinel(conn) is True
        # Single row only.
        count = await conn.fetchval("SELECT COUNT(*) FROM platform_health")
        assert count == 1
