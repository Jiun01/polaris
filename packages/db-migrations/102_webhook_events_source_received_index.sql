-- Migration 102 — index webhook_events on (source, received_at).
-- System Health dashboard enrichment (2026-06-09).
--
-- The admin System Health endpoint (GET /api/admin/health) added two
-- webhook-surface tiles (Telegram + Meta) that aggregate webhook_events
-- by source:
--
--   SELECT MAX(received_at) ... WHERE source = 'telegram'
--   SELECT COUNT(*) ...       WHERE source IN ('meta','wa-status',...)
--                              AND received_at >= NOW() - INTERVAL '1 hour'
--
-- webhook_events is append-only and high-volume (one row per inbound
-- webhook). The existing indexes are (tenant_id, received_at) and
-- (status, received_at) plus the UNIQUE (source, external_id). None of
-- them lets Postgres satisfy a source-filtered MAX(received_at) or a
-- source+time-window COUNT without reading every row for that source
-- from the heap — a cost that grows unbounded and runs every 60 s (the
-- health cache TTL). This composite index makes:
--   - MAX(received_at) WHERE source = ?  → index-only fast lookup
--   - COUNT(*) WHERE source = ? AND received_at >= ? → index range scan
--
-- Mirrors the leading-column shape of the Phase-2 indexes in migration 015.
--
-- NOTE on locking: this uses a plain CREATE INDEX (matching the repo
-- convention in 015_webhook_events.sql), which takes a brief exclusive
-- lock while building. Webhook volume is currently low so this is
-- acceptable. If applied against a large/busy table later, build it
-- out-of-band with CREATE INDEX CONCURRENTLY (cannot run inside a
-- transaction) instead.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP INDEX IF EXISTS webhook_events_source_received_idx;

CREATE INDEX IF NOT EXISTS webhook_events_source_received_idx
  ON webhook_events (source, received_at DESC);

INSERT INTO schema_migrations (filename)
VALUES ('102_webhook_events_source_received_index.sql')
ON CONFLICT (filename) DO NOTHING;
