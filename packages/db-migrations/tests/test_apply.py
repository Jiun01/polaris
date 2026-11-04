"""Tests for apply.py — PR-3 hardened: txn wrap, --dry-run, --json.

DB integration tests require POLARIS_TEST_DATABASE_URL; skipped if unset.
Argparse/JSON tests run unconditionally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[1]
APPLY_PY = MIGRATIONS_DIR / "apply.py"


# ---------- unit-ish tests (no DB) ----------


def _run(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Don't leak prod DSN into unit tests
    env.pop("POLARIS_DATABASE_URL", None)
    env.pop("SUPABASE_DB_URL", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(  # noqa: S603 — fixed argv
        [sys.executable, str(APPLY_PY), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_help_flag_exits_zero() -> None:
    proc = _run(["--help"])
    assert proc.returncode == 0
    assert "polaris-migrate" in proc.stdout
    assert "--dry-run" in proc.stdout
    assert "--json" in proc.stdout


def test_dry_run_without_dsn_prints_full_plan() -> None:
    """--dry-run should never require a DSN; plan treats all as pending."""
    proc = _run(["--dry-run"])
    assert proc.returncode == 0, proc.stderr
    assert "would-apply 001_extensions.sql" in proc.stdout
    assert "dry-run" in proc.stdout


def test_dry_run_json_output_shape() -> None:
    proc = _run(["--dry-run", "--json"])
    assert proc.returncode == 0, proc.stderr
    # JSON line is the only stdout line in quiet mode
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert "001_extensions.sql" in payload["applied"]
    assert payload["skipped"] == []


def test_missing_dsn_real_run_exits_1() -> None:
    proc = _run([])
    assert proc.returncode == 1
    assert "POLARIS_DATABASE_URL" in proc.stderr or "SUPABASE_DB_URL" in proc.stderr


def test_missing_dsn_json_emits_ok_false() -> None:
    proc = _run(["--json"])
    assert proc.returncode == 1
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "error" in payload


# ---------- DB integration tests ----------

db_required = pytest.mark.skipif(
    not os.environ.get("POLARIS_TEST_DATABASE_URL"),
    reason="POLARIS_TEST_DATABASE_URL not set — skipping db integration tests",
)


def _drop_public_schema(dsn: str) -> None:
    """DROP + recreate the public schema so apply.py sees a fresh DB.

    The pr-check.yml workflow runs ``Apply DB migrations`` BEFORE pytest
    (so the API integration tests can run against an applied schema).
    That leaves this test's "fresh-DB" assumption violated — every
    migration would log ``skip`` instead of ``apply``. Reset here so
    the first ``_run`` actually applies, then the test's own second
    ``_run`` proves idempotency. The test re-applies fully, so
    downstream tests in the same pytest invocation still see a fully-
    migrated schema.
    """
    # asyncpg under asyncio.run keeps this file sync — the rest of
    # the test suite doesn't use pytest-asyncio. psycopg2 would be a
    # cleaner sync client but it's not in the workspace deps.
    import asyncio

    import asyncpg

    async def _do() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DROP SCHEMA public CASCADE")
            await conn.execute("CREATE SCHEMA public")
        finally:
            await conn.close()

    asyncio.run(_do())


@db_required
def test_apply_fresh_then_idempotent() -> None:
    env = {"POLARIS_DATABASE_URL": os.environ["POLARIS_TEST_DATABASE_URL"]}
    # Ensure FRESH state — the CI workflow's pre-pytest "Apply DB
    # migrations" step would otherwise put us in "all-skipped" mode.
    _drop_public_schema(env["POLARIS_DATABASE_URL"])

    first = _run([], env_extra=env)
    assert first.returncode == 0, first.stderr
    assert "apply 001_extensions.sql" in first.stdout

    second = _run([], env_extra=env)
    assert second.returncode == 0, second.stderr
    assert "apply 001_extensions.sql" not in second.stdout
    assert "skip  001_extensions.sql" in second.stdout
    assert "0 new migration(s)" in second.stdout


@db_required
def test_json_after_apply_lists_skipped() -> None:
    env = {"POLARIS_DATABASE_URL": os.environ["POLARIS_TEST_DATABASE_URL"]}
    _run([], env_extra=env)  # ensure applied
    proc = _run(["--json"], env_extra=env)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert "001_extensions.sql" in payload["skipped"]
    assert payload["applied"] == []


@db_required
def test_dry_run_against_live_db_reports_what_pending() -> None:
    env = {"POLARIS_DATABASE_URL": os.environ["POLARIS_TEST_DATABASE_URL"]}
    _run([], env_extra=env)  # apply
    proc = _run(["--dry-run", "--json"], env_extra=env)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "001_extensions.sql" in payload["skipped"]
    assert payload["applied"] == []
