-- Migration 112 — repair double-encoded tenants.settings.
-- Ref: systemic JSONB double-encoding fix (writer hardening across ~15 sites in
--      apps/api/polaris_api/services + routers + webhooks).
--
-- Cause: the anon/service pools register a JSONB type codec (encoder=json.dumps)
-- in apps/api/polaris_api/db/pools.py::_init_conn. routers/admin_tenants.py
-- (tenant create) passed json.dumps(initial_settings) to a $N::jsonb param, so
-- the codec re-encoded the already-serialised string and Postgres stored a JSONB
-- *string* (jsonb_typeof='string') instead of an object. The whole-DB audit
-- found this on exactly ONE tenants row (the other affected columns — broadcasts,
-- flow_runs, tenant_invoices, sales_quotes, webhook_events_outbound,
-- tenant_channels.messenger_*, tenants.business_hours — were empty or already
-- correct, so only this column needs a data repair).
--
-- Impact of a string-shaped settings: `settings->>'key'` returns NULL (key
-- extraction on a JSONB string yields NULL), and the PATCH builder in
-- routers/tenant_settings.py does `settings || jsonb_build_object(...)`, which on
-- a string operand yields a 2-element ARRAY instead of a merged object. Readers
-- of tenants.settings already defend against the str-vs-object ambiguity (every
-- one guards `if isinstance(raw, str): json.loads(...)`), so this repair makes
-- the fast object path live and removes the array-merge landmine.
--
-- The writers now pass dicts DIRECTLY (single-encode). This migration repairs the
-- row already written in the broken shape: unwrap the inner JSON text via
-- `#>> '{}'` and re-cast to jsonb.
--
-- Scope + safety:
--   * Only rows whose settings is currently a JSONB string are touched.
--   * Idempotent — after repair jsonb_typeof becomes 'object'; a re-run matches
--     nothing. NULL settings is skipped (jsonb_typeof(NULL) IS NULL).
--   * On a fresh DB (CI migration-check) it affects zero rows.
--   * Assumption: every such string is a double-encoded JSON object (the only way
--     admin_tenants ever wrote it). If a row's inner text were not valid JSON the
--     ::jsonb cast would error, but no such rows exist.
--
-- Rollback: none meaningful (re-double-encoding repaired data is not useful).

UPDATE tenants
   SET settings = (settings #>> '{}')::jsonb
 WHERE jsonb_typeof(settings) = 'string';

INSERT INTO schema_migrations (filename)
VALUES ('112_repair_tenants_settings_double_encode.sql')
ON CONFLICT (filename) DO NOTHING;
