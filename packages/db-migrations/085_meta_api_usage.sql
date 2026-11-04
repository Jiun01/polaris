-- Migration 085 — meta_api_usage telemetry table (System Health Meta tile).
--
-- The admin System Health "Meta API rate-limit headroom" tile
-- (routers/admin_health.py::_probe_meta_rate_limits) has always read FROM
-- meta_api_usage, but no migration ever created the table and nothing wrote to
-- it — so the probe raised "relation does not exist", was swallowed by the
-- per-signal safe-wrapper, and the tile rendered permanently empty (a silent
-- failure). This creates the table + the writer is services/meta_usage.py
-- (best-effort parse of the X-Business-Use-Case-Usage response header captured
-- in services/meta_whatsapp.py on every Graph API send).
--
-- Platform-internal telemetry: written and read exclusively by the service pool
-- (get_service_pool, BYPASSRLS), never by polaris_anon — so no RLS policy is
-- needed and no anon grant is issued. Append-only + idempotent.

CREATE TABLE IF NOT EXISTS meta_api_usage (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    surface             TEXT             NOT NULL,
    calls_remaining_pct DOUBLE PRECISION NOT NULL,
    raw                 JSONB,
    observed_at         TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- The probe filters by observed_at (last 24 h) then groups by surface.
CREATE INDEX IF NOT EXISTS idx_meta_api_usage_observed_at
    ON meta_api_usage (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_meta_api_usage_surface_observed
    ON meta_api_usage (surface, observed_at DESC);

GRANT SELECT, INSERT ON meta_api_usage TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('085_meta_api_usage.sql')
ON CONFLICT (filename) DO NOTHING;
