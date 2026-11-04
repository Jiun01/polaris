-- Migration 040 — relax messages.model_used CHECK to permit "handoff".
-- Phase audit-followup-fixes / 2026-05-19. Ref: docs/Polaris V2/_audits/
--   2026-05-19_phase4_5_agent_architecture_audit.md (finding L-1).
-- Anchors: ADR-031 Amendment 2026-05-15 (model_used vocabulary).
--
-- Before this migration, handoff replies were tagged as
-- model_used='haiku' because the literal didn't include a dedicated
-- token. That polluted the Phase 9 dashboard's per-model counters:
-- every handoff (a hardcoded template — NO LLM call) was rendered as a
-- Haiku call in cost / volume charts. Adding 'handoff' to both the
-- shared-types Literal and the column CHECK keeps the on-disk shape
-- aligned with the application-layer vocabulary.
--
-- Idempotent: drops the existing constraint by name (matches the form
-- migration 028 emitted), then re-adds it with the expanded set.
-- Apply order is enforced by apply.py via the schema_migrations table.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE messages DROP CONSTRAINT messages_model_used_check;
--   ALTER TABLE messages ADD CONSTRAINT messages_model_used_check
--     CHECK (model_used IS NULL OR model_used IN
--            ('haiku','sonnet','gpt-4o-mini-fallback'));

ALTER TABLE messages
  DROP CONSTRAINT IF EXISTS messages_model_used_check;

ALTER TABLE messages
  ADD CONSTRAINT messages_model_used_check
  CHECK (
    model_used IS NULL OR
    model_used IN ('haiku', 'sonnet', 'gpt-4o-mini-fallback', 'handoff')
  );

INSERT INTO schema_migrations (filename) VALUES ('040_messages_model_used_handoff.sql')
ON CONFLICT (filename) DO NOTHING;
