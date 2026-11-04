/**
 * Demo System UI types — ADR-066.
 *
 * HAND-MAINTAINED BY DESIGN, not a TODO: re-confirmed during
 * audit-batch-f-frontend-consolidation (2026-07) that no `Demo*` Pydantic
 * model exists in packages/shared-types — admin_demos.py's response shapes
 * were never exported there. Mirrors the shapes returned by the
 * admin_demos router (`/api/admin/demos`); keep in sync manually. Re-check
 * packages/shared-types/python/polaris_shared_types/ before adding a new
 * hand-maintained duplicate elsewhere.
 */

export interface DemoSummary {
  id: string;
  slug: string;
  display_name: string;
  demo_code: string | null;
  duration_hours: number;
  max_messages: number;
  active_sessions: number;
  created_at: string;
}

export interface DemoSessionRow {
  channel: string;
  channel_user_id: string;
  demo_code: string;
  message_count: number;
  max_messages: number;
  is_expired: boolean;
  lead_flagged: boolean;
  activated_at: string;
  expires_at: string;
}
