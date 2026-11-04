/**
 * Audit-log helper types for the admin impersonation surface (Phase 9 PR-5).
 *
 * AuditLogItem/AuditLogsResponse: HAND-MAINTAINED BY DESIGN, not a TODO —
 * no `AuditLog*` Pydantic model exists in packages/shared-types as of
 * 2026-07 (admin_audit.py's `_AuditLogItem`/`_AuditLogsResponse` are
 * router-local, never exported to shared-types). Re-check
 * packages/shared-types/python/polaris_shared_types/ before adding a new
 * hand-maintained duplicate if this ever needs to move.
 */

export interface AuditLogItem {
  id: string;
  tenant_id: string;
  actor: string | null;
  action: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  impersonating_kind: string | null;
  impersonating_id: string | null;
  created_at: string;
}

export interface AuditLogsResponse {
  items: AuditLogItem[];
  next_cursor: string | null;
}

/**
 * FIXED 2026-07 (audit-batch-f-frontend-consolidation, X2): the two
 * broken-hash fields were previously `broken_prev_hash`/`broken_actual_hash`
 * (no suffix) — silently wrong. Confirmed against admin_audit.py's
 * `_VerificationResponse` (the real response_model for
 * GET /api/admin/audit/verifications/latest) AND
 * polaris_shared_types.audit_chain_verifications.AuditChainVerification
 * (field-identical, though the router uses its own private class, not this
 * one, so it's not re-exported from @polaris/shared-types here) that the
 * real wire field names are hex-suffixed: `broken_prev_hash_hex` /
 * `broken_actual_hash_hex` (the DB column is raw BYTEA; the router
 * hex-encodes it for JSON transport — see audit_chain_verifications.py's
 * docstring). The old un-suffixed names meant
 * `apps/admin/app/(admin)/audit/verify/page.tsx` was reading `undefined`
 * for both fields on every broken-chain render, always showing "—" instead
 * of the actual hash mismatch — a real (if low-traffic) bug, not just a
 * naming nit. Fixed here; page.tsx updated to match.
 */
export interface VerificationResult {
  id: string;
  checked_at: string;
  status: "intact" | "broken" | "errored";
  rows_scanned: number;
  duration_ms: number;
  broken_at_id: string | null;
  broken_at_created_at: string | null;
  broken_prev_hash_hex: string | null;
  broken_actual_hash_hex: string | null;
  triggered_by: "scheduled" | "on_demand";
  triggered_by_admin_id: string | null;
  notes: string | null;
}
