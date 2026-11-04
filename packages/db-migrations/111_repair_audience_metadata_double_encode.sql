-- Migration 111 — repair double-encoded audience_members.metadata.
-- Ref: JSONB double-encoding bug; sibling code fix in
--      apps/api/polaris_api/services/audience/members.py + importer.py.
--
-- Cause: the anon + service pools register a JSONB type codec
-- (encoder=json.dumps) in apps/api/polaris_api/db/pools.py::_init_conn.
-- upsert_audience_member + the CSV importer passed json.dumps(metadata) to a
-- $N::jsonb param, so the codec re-encoded the already-serialised string and
-- Postgres stored a JSONB *string* (e.g. the text `"{}"`, jsonb_typeof='string')
-- instead of an object. Two observable breakages:
--   * metadata->>'intent' audience filters (routers/tenant_audience.py) match
--     nothing — key extraction on a JSONB string returns NULL.
--   * the importer's `metadata || $N::jsonb` merge yields a 2-element ARRAY
--     (['"{}"', {...}]) instead of a merged object.
-- (Verified against Postgres: every existing audience_members.metadata row was
-- the double-encoded text `"{}"`.)
--
-- The code now passes the dict DIRECTLY (single-encode). This migration repairs
-- the rows already written in the broken shape: unwrap the inner JSON text via
-- `#>> '{}'` (which, for a JSONB string, returns its textual content — the
-- original JSON) and re-cast to jsonb, yielding the intended object.
--
-- Scope + safety:
--   * Only rows whose metadata is currently a JSONB string are touched.
--   * Idempotent — after repair jsonb_typeof becomes 'object', so a re-run
--     matches nothing (and the migration runner records the filename anyway).
--   * On a fresh DB (CI migration-check) it affects zero rows.
--   * Assumption: every such string is a double-encoded JSON value — the only
--     way the column was ever written. If a row's inner text were not valid
--     JSON the ::jsonb cast would error, but no such rows exist.
--
-- Rollback: none meaningful (re-double-encoding repaired data is not useful).

UPDATE audience_members
   SET metadata = (metadata #>> '{}')::jsonb
 WHERE jsonb_typeof(metadata) = 'string';

INSERT INTO schema_migrations (filename)
VALUES ('111_repair_audience_metadata_double_encode.sql')
ON CONFLICT (filename) DO NOTHING;
