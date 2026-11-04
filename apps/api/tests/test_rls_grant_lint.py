"""CI lint regression — RLS-policy <-> GRANT pairing gate.

Runs `apps/api/scripts/lint_rls_grants.py` against the live test DB (the
same POLARIS_DATABASE_URL / POLARIS_TEST_DATABASE_URL instance CI's
postgres:17 service provides after `apply.py` has run) and asserts it exits
0 against the current schema. Also proves the gate itself is alive: creates
a scratch table with an RLS policy but no GRANT in a temp schema, confirms
the lint's live-catalog query flags it, then drops the scratch schema --
never touching or committing a real migration file.

The DB-backed tests are individually skipped (not module-wide, mirroring
packages/db-migrations/tests/test_apply.py's `db_required` decorator
convention) if no DSN is set; the pure filesystem check
(`test_lint_script_exists`) always runs, mirroring
apps/api/tests/test_pools_lint.py's unconditional version of the same check.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

_LINT_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "lint_rls_grants.py"
)

_DSN_ENV_VARS = ("POLARIS_DATABASE_URL", "SUPABASE_DB_URL", "POLARIS_TEST_DATABASE_URL")


def _dsn() -> str | None:
    for var in _DSN_ENV_VARS:
        val = os.environ.get(var)
        if val:
            return val
    return None


db_required = pytest.mark.skipif(
    _dsn() is None,
    reason="set POLARIS_DATABASE_URL or SUPABASE_DB_URL — skipping db integration tests",
)


def test_lint_script_exists() -> None:
    assert _LINT_SCRIPT.exists(), f"lint script missing at {_LINT_SCRIPT}"


@db_required
def test_current_schema_passes_lint() -> None:
    """Real schema (post apply.py) should be clean — every RLS policy has a
    matching GRANT for polaris_anon and/or polaris_service."""
    dsn = _dsn()
    assert dsn is not None
    env = {**os.environ, "POLARIS_DATABASE_URL": dsn}
    result = subprocess.run(  # noqa: S603 — script path is constant
        [sys.executable, str(_LINT_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (
        f"lint_rls_grants reported orphaned policies:\nSTDOUT: {result.stdout}\n"
        f"STDERR: {result.stderr}"
    )


async def _setup_scratch_orphan(dsn: str, table: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP TABLE IF EXISTS {table}")
        await conn.execute(
            f"CREATE TABLE {table} (id UUID PRIMARY KEY DEFAULT gen_random_uuid())"
        )
        await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        await conn.execute(
            f"CREATE POLICY {table}_self ON {table} FOR SELECT USING (true)"
        )
        # Deliberately NO GRANT — this is the orphan case the lint must catch.
    finally:
        await conn.close()


async def _teardown_scratch_orphan(dsn: str, table: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    finally:
        await conn.close()


@db_required
def test_synthetic_orphan_policy_is_caught() -> None:
    """Add a scratch table with RLS + a policy but zero GRANT, confirm the
    lint's live-catalog query flags it, then clean up unconditionally.

    Sync test (not async def) — this deliberately shells out via
    subprocess.run, which ruff's ASYNC221 rule forbids inside an async test;
    the asyncpg setup/teardown calls run via asyncio.run() instead.
    """
    dsn = _dsn()
    assert dsn is not None
    table = "_lint_rls_grants_scratch_orphan"
    asyncio.run(_setup_scratch_orphan(dsn, table))
    try:
        env = {**os.environ, "POLARIS_DATABASE_URL": dsn}
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(_LINT_SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 1, (
            f"expected the scratch orphan policy to fail the lint, got "
            f"returncode={result.returncode}\nSTDOUT: {result.stdout}"
        )
        assert table in result.stderr
    finally:
        asyncio.run(_teardown_scratch_orphan(dsn, table))
