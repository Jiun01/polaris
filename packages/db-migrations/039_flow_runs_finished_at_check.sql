-- Migration 039 — flow_runs finished_at invariant CHECK (Phase 5 / PR-8).
-- Ref: phase-5 code review M11 in .claude/reviews/phase-5-review.md.
-- Pairs with 034_flow_runs.sql.
--
-- Migration 034 documented the application invariant:
--   terminal_status = 'running' ⇔ finished_at IS NULL
--
-- The runner always writes both columns at the same time (runner.py
-- update_flow_run_terminal sets terminal_status to a non-running
-- value at the same statement that sets finished_at = now()). But
-- migration 034 deliberately omitted a CHECK constraint to avoid
-- tripping legacy rows during partial rollouts.
--
-- At this point there are no legacy rows — Phase 5 only just landed,
-- and the table was created in 034 with terminal_status='running' as
-- the DEFAULT for new inserts. We can safely add the CHECK now:
-- every existing row was either INSERTed by the runner (which then
-- always UPDATEs to a terminal state with finished_at set) or
-- happens to still be running (finished_at NULL).
--
-- The CHECK catches a botched manual UPDATE that would leave the
-- row in a semantically invalid state — e.g. SET terminal_status=
-- 'completed' WHERE id=… without setting finished_at. Such a row
-- would silently model as a valid completed run with unknown finish
-- time, polluting Phase 9's cost dashboard.
--
-- Rollback (local only):
--   ALTER TABLE flow_runs DROP CONSTRAINT flow_runs_finished_at_chk;

ALTER TABLE flow_runs
    ADD CONSTRAINT flow_runs_finished_at_chk
    CHECK ((terminal_status = 'running') = (finished_at IS NULL));

INSERT INTO schema_migrations (filename) VALUES ('039_flow_runs_finished_at_check.sql')
ON CONFLICT (filename) DO NOTHING;
