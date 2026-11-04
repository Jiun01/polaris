"""Diagnose schema_migrations vs filesystem drift.

Lists what's tracked in the schema_migrations table vs what's in
packages/db-migrations/ on disk. Highlights tracking drift (file
present + not tracked, or tracked + not present).

No writes. Safe to run any time.

Usage:
  uv run python -m apps.api.scripts.check_schema_migrations
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncpg  # noqa: E402

try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(_REPO_ROOT / ".env", override=True)
except ImportError:
    pass


async def main() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("POLARIS_DATABASE_URL")
    if not dsn:
        print("ERROR: SUPABASE_DB_URL not set")
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT filename FROM schema_migrations ORDER BY filename"
        )
        tracked = {r["filename"] for r in rows}
    finally:
        await conn.close()

    files_dir = _REPO_ROOT / "packages" / "db-migrations"
    on_disk = {p.name for p in files_dir.glob("*.sql")}

    print(f"schema_migrations rows: {len(tracked)}")
    print(f"files on disk:          {len(on_disk)}")
    print()

    missing_in_db = on_disk - tracked
    if missing_in_db:
        print(f"** {len(missing_in_db)} file(s) present on disk but NOT tracked in schema_migrations **")
        for f in sorted(missing_in_db):
            print(f"  - {f}")
        print()

    missing_on_disk = tracked - on_disk
    if missing_on_disk:
        print(f"** {len(missing_on_disk)} row(s) in schema_migrations but file NOT on disk **")
        for f in sorted(missing_on_disk):
            print(f"  - {f}")
        print()

    if not missing_in_db and not missing_on_disk:
        print("OK: schema_migrations matches the filesystem exactly.")
        return 0

    print("Diagnosis hint:")
    print("  - If a file IS tracked but its CREATE TABLE didn't run, the DDL")
    print("    succeeded then the INSERT INTO schema_migrations was rolled")
    print("    back (rare — apply.py wraps both in one transaction).")
    print("  - If a file is NOT tracked but its DDL clearly ran (table exists)")
    print("    that means a previous apply.py crashed mid-run after DDL but")
    print("    before tracking. Re-running apply.py will try to apply that")
    print("    SQL again — the migrations should be written to be idempotent")
    print("    (CREATE TABLE IF NOT EXISTS) so this usually works, but check")
    print("    the migration body.")
    print("  - Inspect actual table state via psql:")
    print("      \\dt   -- list tables")
    print("      SELECT * FROM information_schema.tables WHERE table_schema='public';")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
