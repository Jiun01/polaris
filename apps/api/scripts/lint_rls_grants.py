"""CI lint — every RLS-enabled table must have a GRANT for a role with an
active policy on it.

Guards against the 118 -> 119 bug class: migration 118 enabled RLS + added
policies on knowledge_sources but shipped with ZERO GRANTs, so polaris_anon
got "permission denied" on every query despite the policies being logically
correct -- RLS is enforced ON TOP OF table privileges, never instead of them.

Queries pg_policies + information_schema.role_table_grants directly against
whatever Postgres this is pointed at (CI's postgres:17 service after
apply.py has run, or a local dev DB). For every table that has at least one
row in pg_policies (i.e. RLS is actively used, not just enabled-but-unused),
assert at least one non-empty GRANT exists for polaris_anon and/or
polaris_service (whichever app role(s) actually query tenant-scoped tables) --
a table with a policy but zero GRANT to either role is an orphaned policy:
logically present, structurally useless, and a silent 403/500 waiting to
happen the moment a route queries it.

An empty pg_policies result (e.g. against a bare, unmigrated schema) is
treated as a pass, not a failure -- there's nothing to check yet.

Exit 0 = clean. Exit 1 = orphaned policy (RLS without GRANT) found, with
table name(s) printed to stderr.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

# The two application roles every RLS-scoped table is expected to serve.
# Mirrors the posture documented across the migration grants files (e.g.
# 119_knowledge_sources_grants.sql, 045_phase6_grants.sql): polaris_anon for
# tenant-request-scoped access, polaris_service for BYPASSRLS worker/cron
# access. A table's policies may target only one of the two, but the query
# below only requires *a* GRANT to *either* role -- it flags total absence,
# not which specific role is missing (that's a design choice per table).
_APP_ROLES: tuple[str, ...] = ("polaris_anon", "polaris_service")

_ORPHAN_QUERY = """
SELECT DISTINCT p.tablename
FROM pg_policies p
WHERE NOT EXISTS (
  SELECT 1 FROM information_schema.role_table_grants g
  WHERE g.table_name = p.tablename
    AND g.grantee = ANY($1::text[])
)
ORDER BY p.tablename;
"""


def _dsn_from_env() -> str | None:
    # Same lookup order as packages/db-migrations/apply.py's _dsn_from_env --
    # kept as a local copy (not imported) since apply.py isn't a package this
    # script can cleanly import from CI's working directory.
    return os.environ.get("POLARIS_DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")


async def _find_orphans(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_ORPHAN_QUERY, list(_APP_ROLES))
    finally:
        await conn.close()
    return [r["tablename"] for r in rows]


def main() -> int:
    dsn = _dsn_from_env()
    if not dsn:
        print(
            "lint_rls_grants: set POLARIS_DATABASE_URL or SUPABASE_DB_URL",
            file=sys.stderr,
        )
        return 1

    try:
        orphans = asyncio.run(_find_orphans(dsn))
    except Exception as exc:  # noqa: BLE001 — surface any connection/query error as a lint failure
        print(f"lint_rls_grants: DB check failed: {exc}", file=sys.stderr)
        return 1

    if not orphans:
        return 0

    print(
        "lint_rls_grants: tables with an RLS policy but no GRANT to "
        f"{' or '.join(_APP_ROLES)}:",
        file=sys.stderr,
    )
    for table in orphans:
        print(f"  {table}", file=sys.stderr)
    print(
        "\nRLS policies are enforced ON TOP OF table privileges -- a policy\n"
        "without a matching GRANT is a silent permission-denied waiting to\n"
        "happen. Add a companion `_grants.sql` migration (see the\n"
        "118_knowledge_sources.sql -> 119_knowledge_sources_grants.sql pair).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
