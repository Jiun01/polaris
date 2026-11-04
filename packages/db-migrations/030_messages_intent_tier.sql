-- Migration 030 — messages.intent_tier (Phase 4 PR-7 / code-review fix C-1).
-- Ref: docs/Polaris V2/04e_PHASE_4_DEEP.md §10 + .claude/reviews/pr-25-review.md.
--
-- Two different "intent" concepts had the same name + collided:
--
--   * ``messages.intent`` (from migration 028 + PRD §10.9 / SSOT §4.6) =
--     the SEMANTIC intent label, populated by Phase 5 flow routing.
--     Vocabulary: ('greeting','faq','booking','handoff','promo','other').
--
--   * ``IntentTier`` from packages/shared-types/.../llm_response.py =
--     the CLASSIFIER TIER label that gates model + max_tokens routing.
--     Vocabulary: ('simple','medium','hard','handoff').
--
-- Only ``'handoff'`` overlaps. The Phase 4 PR-4 pipeline orchestrator
-- mistakenly wrote ``response.intent`` (the tier) into ``messages.intent``
-- (the semantic field), which would fail the CHECK constraint on every
-- non-handoff reply. Unit tests missed it because they mocked the DB
-- layer; only a real Postgres integration exercise would have tripped
-- the CHECK violation.
--
-- This migration adds a dedicated ``intent_tier`` column for the
-- classifier output. Phase 5 keeps the SSOT ``intent`` column for
-- semantic flow routing; Phase 4 leaves ``intent`` NULL until then
-- (the pipeline never has to guess a semantic label from a tier).
--
-- Rollback (local only):
--   ALTER TABLE messages DROP COLUMN IF EXISTS intent_tier;

ALTER TABLE messages
  ADD COLUMN intent_tier TEXT NULL CHECK (
    intent_tier IS NULL OR
    intent_tier IN ('simple','medium','hard','handoff')
  );

-- No index — Phase 9 dashboard queries the column via per-message
-- aggregation, not point lookups. Add an index then if recall pain
-- shows up in production telemetry.

INSERT INTO schema_migrations (filename) VALUES ('030_messages_intent_tier.sql')
ON CONFLICT (filename) DO NOTHING;
