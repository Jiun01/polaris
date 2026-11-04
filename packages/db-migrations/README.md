# packages/db-migrations

Idempotent SQL migration files + `apply.py` runner.

## Conventions

- Files are named `NNN_<short_slug>.sql` (e.g. `001_extensions.sql`).
- **Append-only.** Never edit a merged migration file. Bug fixes ship as a new numbered file.
- Every migration is wrapped in a single statement run; PR-3 adds transaction wrapping.
- Migrations are tracked by filename in the `schema_migrations` table.

## Running locally

```bash
# Set DSN
export POLARIS_DATABASE_URL='postgres://user:pass@localhost:5432/postgres'

# Or for Supabase
export SUPABASE_DB_URL='postgres://postgres:[PASSWORD]@db.<ref>.supabase.co:5432/postgres'

uv run python packages/db-migrations/apply.py
```

## Tests

```bash
uv run pytest packages/db-migrations/tests -v
```

Integration test spins up a fresh `postgres:17` and asserts idempotence
(`apply.py` run twice yields zero applied on the second run).
