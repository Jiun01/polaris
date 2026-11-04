-- Migration 025 — ingestion_requests.priority (Phase 3 / PR-3).
-- Ref: docs/Polaris V2/04d_PHASE_3_DEEP.md §3.8 + §6.5 + PRD §6.2.1.
-- Anchors: Q-PHASE3-11 (hybrid cron + GUI + force-run — priority enum
--          enables force-run by flagging rows the desktop drains first).
--
-- Adds a two-value priority enum + column to ingestion_requests so the
-- POST /api/admin/ingestion/force-run endpoint can flip flagged rows to
-- `high`. The desktop's pop_pending() orders by (priority DESC,
-- submitted_at ASC), draining high-priority rows on the next cron tick
-- or GUI "Run Now" press.
--
-- Replaces the hot-read partial index from migration 018 with a
-- priority-aware variant so the queue-drain query plan stays cheap
-- regardless of how many force-run rows are flagged.
--
-- Rollback (local only):
--   DROP INDEX IF EXISTS ingestion_requests_status_priority_submitted_idx;
--   CREATE INDEX ingestion_requests_status_submitted_idx
--     ON ingestion_requests (status, submitted_at)
--     WHERE status IN ('pending', 'processing');
--   ALTER TABLE ingestion_requests DROP COLUMN IF EXISTS priority;
--   DROP TYPE IF EXISTS ingestion_priority;

CREATE TYPE ingestion_priority AS ENUM ('normal', 'high');

ALTER TABLE ingestion_requests
  ADD COLUMN priority ingestion_priority NOT NULL DEFAULT 'normal';

-- Swap the partial index so the desktop's pop_pending() uses an index
-- that already has priority sorted DESC. Without this swap the query
-- planner would do an extra sort step per drain cycle.
DROP INDEX IF EXISTS ingestion_requests_status_submitted_idx;
CREATE INDEX ingestion_requests_status_priority_submitted_idx
  ON ingestion_requests (status, priority DESC, submitted_at)
  WHERE status IN ('pending', 'processing');

INSERT INTO schema_migrations (filename) VALUES ('025_ingestion_requests_priority.sql')
ON CONFLICT (filename) DO NOTHING;
