"use client";

/**
 * Follow-up toggle (Phase 13 follow-ups).
 *
 * One switch: whether Polaris sends an AI re-engagement follow-up when a
 * customer goes quiet inside WhatsApp's free 24h window. Self-contained —
 * fetches + saves its own blob via /api/tenant/followups/config, so it
 * drops onto the Settings page without threading through the sectioned
 * settings form. Autosave-on-toggle (a single boolean; no Save button
 * needed). Quiet hours + timing are Polaris defaults — no knobs here.
 */

import * as React from "react";

import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

interface FollowupConfigDTO {
  enabled: boolean;
}

export function FollowupSettingsCard() {
  const [enabled, setEnabled] = React.useState(false);
  const [loaded, setLoaded] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [savedAt, setSavedAt] = React.useState<number | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let active = true;
    apiFetch<FollowupConfigDTO>(TenantRoutes.followupsConfig())
      .then((cfg) => {
        if (active && cfg) setEnabled(cfg.enabled);
      })
      .catch(() => {
        /* leave default off; any real error surfaces on save */
      })
      .finally(() => {
        if (active) setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  async function save(next: boolean) {
    setSaving(true);
    setError(null);
    try {
      const saved = await apiFetch<FollowupConfigDTO>(
        TenantRoutes.followupsConfig(),
        { method: "PUT", body: JSON.stringify({ enabled: next }) },
      );
      if (saved) setEnabled(saved.enabled);
      setSavedAt(Date.now());
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't save. Try again.",
      );
      setEnabled(!next); // revert the optimistic flip
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border bg-card p-5">
      <div>
        <h2 className="text-lg font-medium">Automatic follow-ups</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          When a customer goes quiet mid-chat, Polaris sends one friendly AI
          follow-up later the same day, inside WhatsApp&apos;s free 24-hour
          window, never at night, and never after a teammate takes over.
          WhatsApp only.
        </p>
      </div>
      <div className="flex items-center gap-3 rounded-md border bg-muted/20 p-3">
        <Switch
          id="followup-enabled"
          checked={enabled}
          disabled={!loaded || saving}
          onCheckedChange={(checked) => {
            setEnabled(checked);
            setSavedAt(null);
            void save(checked);
          }}
        />
        <Label htmlFor="followup-enabled" className="text-sm font-normal">
          {enabled ? "On, sending follow-ups" : "Off"}
        </Label>
        {saving ? (
          <span className="text-xs text-muted-foreground">Saving…</span>
        ) : null}
        {savedAt !== null && !saving ? (
          <span className="text-xs text-emerald-700 dark:text-emerald-400">
            Saved.
          </span>
        ) : null}
      </div>
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </section>
  );
}
