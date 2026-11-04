-- Migration 044 — tenant_channels MM Lite onboarding columns.
-- Phase 6 / PR-1. Ref: docs/Polaris V2/04g_PHASE_6_DEEP.md §3.4 +
--   00a §6.10 (MM Lite routing).
-- Anchors: ADR-034 (channel onboarding paste-only at GA — MM Lite onboarding
--   webhook updates these columns when Meta fires AD_ACCOUNT_LINKED /
--   ONBOARDING_COMPLETED for a phone_number_id we host).
--
-- mm_lite_status already exists from migration 004 (Phase 1 / PR-1). The
-- allowed values are managed in application code, not via a DB CHECK:
--   NULL          — never onboarded; never attempted
--   'NOT_STARTED' — Meta-acknowledged the WABA but no MM Lite flow started
--   'IN_PROGRESS' — onboarding flow began (typically AD_ACCOUNT_LINKED)
--   'ONBOARDED'   — onboarding completed; MARKETING templates may use
--                   /marketing_messages endpoint per services/broadcast/
--                   service._resolve_send_path()
--   'UNLINKED'    — Meta unlinked the ad account (regression from ONBOARDED)
-- Application-code enforcement (not CHECK) is intentional: Meta may
-- introduce new states; graceful pass-through beats migration churn.
--
-- Three columns added here:
--   mm_lite_status_updated_at — every status flip stamps this; powers
--     "stale onboarding" sweep + admin dashboard "last MM Lite refresh"
--   mm_lite_onboarded_at      — first transition into 'ONBOARDED'; never
--     overwritten on subsequent flips. Used to compute MM Lite eligibility
--     duration for the cost dashboard (per finance-billing-ops audit).
--   mm_lite_ad_account_id     — Meta's AD account binding; surfaced in the
--     wa-account webhook payload (AD_ACCOUNT_LINKED.value.ad_account_id).
--     Stored opaque — Polaris doesn't decode it.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenant_channels
--     DROP COLUMN IF EXISTS mm_lite_status_updated_at,
--     DROP COLUMN IF EXISTS mm_lite_onboarded_at,
--     DROP COLUMN IF EXISTS mm_lite_ad_account_id;

ALTER TABLE tenant_channels
  ADD COLUMN mm_lite_status_updated_at TIMESTAMPTZ NULL,
  ADD COLUMN mm_lite_onboarded_at      TIMESTAMPTZ NULL,
  ADD COLUMN mm_lite_ad_account_id     TEXT NULL;

INSERT INTO schema_migrations (filename) VALUES ('044_tenant_channels_mm_lite_columns.sql')
ON CONFLICT (filename) DO NOTHING;
