-- Migration 048: fix tenant_templates.name CHECK constraint regex.
--
-- The original constraint in 041 used `name ~ '^[a-z0-9_]{1,512}$'`.
-- PostgreSQL's POSIX regex engine caps repetition counts at 255, so
-- {1,512} raises "invalid regular expression: invalid repetition count(s)"
-- on all Supabase-hosted Postgres builds tested (PG 17.6, aarch64).
--
-- Fix: split into a length check (handles the 512-char cap) + a simple
-- pattern regex with no repetition quantifier ({1,+} → bare +).
-- Semantics are identical: 1–512 chars, all lowercase alphanum or underscore.
--
-- Rollback note: DROP the new constraint + re-add the old one (but only
-- on non-Supabase PG builds where {1,512} is accepted).

ALTER TABLE tenant_templates
    DROP CONSTRAINT IF EXISTS tenant_templates_name_check;

ALTER TABLE tenant_templates
    ADD CONSTRAINT tenant_templates_name_check CHECK (
        length(name) BETWEEN 1 AND 512
        AND name ~ '^[a-z0-9_]+$'
    );

INSERT INTO schema_migrations (filename) VALUES ('048_fix_tenant_templates_name_check.sql')
ON CONFLICT (filename) DO NOTHING;
