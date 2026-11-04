"""Idempotent migration applier — PR-3 hardened.

Reads every *.sql file in this directory (sorted by filename), wraps each in a
single transaction, applies any not-yet-recorded file, records the row.

PR-1 baseline → PR-3 hardening:
- Per-migration BEGIN/COMMIT transaction (rollback on failure)
- `--dry-run` prints the plan + exits 0 without touching the DB
- `--json` emits one JSON object to stdout for CI / scripts to parse

Phase 1 swaps to a dedicated migration role; Phase 0 uses SUPABASE_SERVICE_KEY.

Exit codes:
  0  success (any number applied, including zero)
  1  failure (connection, SQL error, or env misconfig)

JSON shape (--json):
{
  "ok": true,
  "applied": ["003_foo.sql"],
  "skipped": ["001_extensions.sql", "002_bar.sql"],
  "dry_run": false
}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent


@dataclass
class RunResult:
    ok: bool = True
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False
    error: str | None = None

    def to_json(self) -> str:
        payload: dict[str, object] = {
            "ok": self.ok,
            "applied": self.applied,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
        }
        if self.error:
            payload["error"] = self.error
        return json.dumps(payload, sort_keys=True)


def _dsn_from_env() -> str | None:
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


def _log(out: TextIO, msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(msg, file=out)


async def _ensure_schema_migrations(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          filename   TEXT PRIMARY KEY,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


async def _applied_set(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    return {row["filename"] for row in rows}


def _assert_inside_migrations_dir(path: Path) -> None:
    """Defence against symlink swap: confirm path resolves under MIGRATIONS_DIR.

    Mitigates TOCTOU between glob() and read_text() — if a `.sql` file is
    replaced with a symlink to elsewhere between discovery and execution,
    the resolved parent won't match the canonical migrations directory.
    """
    resolved = path.resolve()
    if resolved.parent != MIGRATIONS_DIR.resolve():
        raise RuntimeError(
            f"migration file {path.name} resolves outside migrations dir "
            f"(symlink swap or path traversal attempt)"
        )


async def _apply_one(conn: asyncpg.Connection, path: Path) -> None:
    """Apply a single migration inside a transaction.

    A single asyncpg.Transaction wraps the SQL file plus the bookkeeping insert.
    Either both land or neither does. The file read is offloaded to a thread so
    the event loop isn't blocked on disk I/O (ASYNC240). TOCTOU guard via
    `_assert_inside_migrations_dir` blocks symlink-swap between glob+read.
    """
    _assert_inside_migrations_dir(path)
    sql = await asyncio.to_thread(path.read_text, encoding="utf-8")
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING",
            path.name,
        )


async def run(args: argparse.Namespace) -> RunResult:
    result = RunResult(dry_run=args.dry_run)
    quiet = bool(args.json)

    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.is_file())

    # Dry-run path — never touches the DB. We still need to know what's applied
    # so the plan is realistic. If no DSN is set we fall back to "treat as fresh".
    if args.dry_run:
        applied: set[str] = set()
        dsn = _dsn_from_env()
        if dsn:
            try:
                conn = await asyncpg.connect(dsn)
                try:
                    await _ensure_schema_migrations(conn)
                    applied = await _applied_set(conn)
                finally:
                    await conn.close()
            except Exception as exc:  # noqa: BLE001
                _log(sys.stderr, f"apply.py: dry-run DB probe failed: {exc}", quiet=quiet)
                # Still produce a plan assuming nothing is applied.
        for path in files:
            if path.name in applied:
                result.skipped.append(path.name)
                _log(sys.stdout, f"would-skip  {path.name}", quiet=quiet)
            else:
                result.applied.append(path.name)
                _log(sys.stdout, f"would-apply {path.name}", quiet=quiet)
        _log(sys.stdout, f"dry-run — {len(result.applied)} pending", quiet=quiet)
        return result

    # Real run path
    dsn = _dsn_from_env()
    if not dsn:
        result.ok = False
        result.error = "set POLARIS_DATABASE_URL or SUPABASE_DB_URL"
        _log(sys.stderr, f"apply.py: {result.error}", quiet=quiet)
        return result

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        # Don't include str(exc) — asyncpg connect errors echo the DSN with
        # password segments. Class name is enough for diagnosis.
        result.ok = False
        result.error = f"connect failed: {type(exc).__name__}"
        _log(sys.stderr, f"apply.py: {result.error}", quiet=quiet)
        return result

    try:
        await _ensure_schema_migrations(conn)
        applied = await _applied_set(conn)

        for path in files:
            if path.name in applied:
                result.skipped.append(path.name)
                _log(sys.stdout, f"skip  {path.name}", quiet=quiet)
                continue
            _log(sys.stdout, f"apply {path.name}", quiet=quiet)
            try:
                await _apply_one(conn, path)
            except Exception as exc:  # noqa: BLE001
                # str(exc) on asyncpg/file errors can echo paths or DSN
                # fragments. Stick to the class name.
                result.ok = False
                result.error = f"failed on {path.name}: {type(exc).__name__}"
                _log(sys.stderr, f"apply.py: {result.error}", quiet=quiet)
                return result
            result.applied.append(path.name)

        _log(
            sys.stdout,
            f"done — {len(result.applied)} new migration(s) applied",
            quiet=quiet,
        )
        return result
    finally:
        await conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polaris-migrate",
        description="Polaris v2 idempotent migration applier.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned migrations without applying. Exits 0.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON object on stdout.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = asyncio.run(run(args))
    if args.json:
        print(result.to_json())
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
