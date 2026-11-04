-- Migration 037 — messages.flow_run_id FK (Phase 5 / PR-1).
-- Ref: docs/Polaris V2/04f_PHASE_5_DEEP.md §3.6 + 04e_PHASE_4_DEEP.md Quick Reference bullet 5.
--
-- The ``messages.flow_run_id`` column was introduced in migration 028
-- (Phase 4) as a forward slot with NO FK constraint, because
-- ``flow_runs`` did not yet exist. Phase 4's invariant kept
-- ``flow_run_id IS NULL`` for every row it wrote, asserted by
-- ``test_messages_persisted`` (PR-4) so the FK migration here cannot
-- trip on orphan IDs.
--
-- The constraint is DEFERRABLE INITIALLY DEFERRED so the Phase 5
-- pipeline path can write a ``messages`` row + a ``flow_runs`` row in
-- either order within the same transaction without the FK check
-- firing mid-statement. The check runs at COMMIT once both rows
-- exist.
--
-- ON DELETE SET NULL means a retention-sweep deletion of an old
-- ``flow_runs`` row (Phase 9) preserves the messages history but
-- detaches the (now-stale) flow-run reference. We keep the messages
-- because conversation history is the customer-facing record; the
-- flow-run is internal audit telemetry whose retention window is
-- shorter.

ALTER TABLE messages
  ADD CONSTRAINT messages_flow_run_id_fk
  FOREIGN KEY (flow_run_id) REFERENCES flow_runs(id) ON DELETE SET NULL
  DEFERRABLE INITIALLY DEFERRED;

INSERT INTO schema_migrations (filename) VALUES ('037_messages_flow_run_id_fk.sql')
ON CONFLICT (filename) DO NOTHING;
