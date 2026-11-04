-- Migration 096 — reconcile the off-Meta widget read-only redesign + the 094
-- telegram-secret CHECK fix, for environments that applied the ORIGINAL 094/095.
--
-- WHY THIS EXISTS (process honesty): migrations 094/095 were edited in place on
-- the feature branch AFTER they had already been applied to the dev Supabase —
-- a violation of the append-only rule. The merged 094/095 are correct for a
-- FRESH apply, but a DB that already ran the originals (where 094/095 are
-- recorded in schema_migrations) will never re-run the edited files. This
-- migration carries the deltas as a proper, idempotent, append-only step so
-- EVERY environment converges via `make migrate` alone — no manual SQL.
--
-- Idempotent + safe on all states:
--   * fresh DB (merged 094/095 already correct)         → both statements no-op
--   * dev DB (manually reconciled earlier)              → both statements no-op
--   * a DB that applied ORIGINAL 094/095, un-reconciled → both statements fix it
--
-- Rollback: not applicable (this only removes a dropped table + re-asserts a
-- constraint that the merged 094 already defines).

-- (1) The original migration 095 created a `widget_click_events` attribution
--     table. The read-only widget redesign dropped attribution entirely — the
--     bubble links directly at t.me/wa.me with no server redirect/write. Remove
--     the table from any DB that still has it.
DROP TABLE IF EXISTS widget_click_events;

-- (2) The original migration 094 constrained `telegram_webhook_secret` with a
--     regex bound `'^[A-Za-z0-9_-]{1,256}$'`. Postgres's regex engine caps `{m,n}`
--     repetition at 255, so `{1,256}` raises "invalid repetition count(s)" — and
--     because the regex compiles LAZILY, it only fired on the first row INSERT
--     (not at constraint creation), so migration-apply silently passed. Re-assert
--     the corrected charset (`+`, unbounded) + length (`char_length`) form.
ALTER TABLE tenant_channels
  DROP CONSTRAINT IF EXISTS tenant_channels_telegram_webhook_secret_charset;

ALTER TABLE tenant_channels
  ADD CONSTRAINT tenant_channels_telegram_webhook_secret_charset
  CHECK (
    telegram_webhook_secret ~ '^[A-Za-z0-9_-]+$'
    AND char_length(telegram_webhook_secret) BETWEEN 1 AND 256
  );

INSERT INTO schema_migrations (filename) VALUES ('096_widget_readonly_reconcile.sql')
ON CONFLICT (filename) DO NOTHING;
