/**
 * System Health page (Phase 9 PR-6).
 *
 * 5-tile layout: Supabase, Redis, Broadcast Queue Depth,
 * Ingestion Last Run (per-tenant table), Meta API Rate Limits.
 *
 * Server Component: does the initial data fetch server-side for fast
 * first paint (≤ 2 s budget), then hands off to <HealthDashboard>
 * (client) for auto-refresh + manual "Refresh now" CTA.
 */

import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { HealthDashboard } from "@/components/health/HealthDashboard";
import type { HealthSnapshot } from "@/lib/types/health";

async function fetchHealthSnapshot(): Promise<HealthSnapshot | null> {
  try {
    const res = await adminServerFetch<HealthSnapshot>(
      adminRoutes.health.snapshot(),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError) {
      // eslint-disable-next-line no-console
      console.error("[health page] API error:", err.status, err.message);
    } else {
      // eslint-disable-next-line no-console
      console.error("[health page] fetchHealthSnapshot:", err);
    }
    return null;
  }
}

export default async function HealthPage() {
  const initialData = await fetchHealthSnapshot();

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            System Health
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Live snapshot of platform infrastructure — auto-refreshes every 5
            minutes.
          </p>
        </div>
      </div>

      <HealthDashboard initialData={initialData} />
    </div>
  );
}
