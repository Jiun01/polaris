/**
 * Health dashboard — client component (Phase 9 PR-6).
 *
 * Renders the 5-tile layout and owns the auto-refresh cycle:
 *   - Refreshes every 5 minutes via setInterval.
 *   - Pauses polling when the browser tab is hidden
 *     (document.visibilitychange + document.hidden).
 *   - Exposes a "Refresh now" button that fires an immediate fetch.
 *   - Displays a "Last refreshed at" timestamp in MYT.
 *
 * Each tile renders independently from its own slice of HealthSnapshot.
 * If the server returns null for the whole response (e.g. 500), every
 * tile falls back gracefully (no crash, just "Unavailable").
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { formatMyt } from "@/lib/format";
import { SupabaseStatusTile } from "@/components/health/SupabaseStatusTile";
import { RedisStatusTile } from "@/components/health/RedisStatusTile";
import { BroadcastQueueDepthTile } from "@/components/health/BroadcastQueueDepthTile";
import { MessagingVolumeTile } from "@/components/health/MessagingVolumeTile";
import { LlmTokenBurnTile } from "@/components/health/LlmTokenBurnTile";
import { QuotaSaturationTile } from "@/components/health/QuotaSaturationTile";
import { RecentFailuresTile } from "@/components/health/RecentFailuresTile";
import { MetaRateLimitTile } from "@/components/health/MetaRateLimitTile";
import { EmailDraftQueueTile } from "@/components/health/EmailDraftQueueTile";
import { KnowledgeGapCapturesTile } from "@/components/health/KnowledgeGapCapturesTile";
import { ActiveTenantsTile } from "@/components/health/ActiveTenantsTile";
import { ServiceConnectivityTile } from "@/components/health/ServiceConnectivityTile";
import { WebhookHealthTile } from "@/components/health/WebhookHealthTile";
import { WebhookEventErrorsTile } from "@/components/health/WebhookEventErrorsTile";
import { ApmErrorsTile } from "@/components/health/ApmErrorsTile";
import type { HealthSnapshot } from "@/lib/types/health";

const REFRESH_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes

interface Props {
  initialData: HealthSnapshot | null;
}

export function HealthDashboard({ initialData }: Props) {
  const [snapshot, setSnapshot] = useState<HealthSnapshot | null>(initialData);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(
    initialData?.refreshed_at ?? null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const doFetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await adminFetch<HealthSnapshot>(
        adminRoutes.health.snapshot(),
      );
      if (data) {
        setSnapshot(data);
        // Use the client-side wall-clock time so that a manual "Refresh now"
        // click always updates the displayed timestamp even when the API
        // returns a cached snapshot (60-second TTL) with the same refreshed_at.
        setLastRefreshedAt(new Date().toISOString());
      }
    } catch (err) {
      // Non-fatal: show a banner but keep the last good snapshot visible.
      setError(
        err instanceof Error ? err.message : "Failed to refresh health data.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  // Set up auto-refresh with tab-visibility gating.
  useEffect(() => {
    function startInterval() {
      if (intervalRef.current) return; // already running
      intervalRef.current = setInterval(() => {
        if (!document.hidden) {
          void doFetch();
        }
      }, REFRESH_INTERVAL_MS);
    }

    function stopInterval() {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    }

    function handleVisibilityChange() {
      if (document.hidden) {
        stopInterval();
      } else {
        // Tab became visible — fetch immediately then restart the timer.
        void doFetch();
        startInterval();
      }
    }

    startInterval();
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      stopInterval();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [doFetch]);

  return (
    <div className="space-y-6">
      {/* Toolbar — last-refreshed timestamp + manual refresh button */}
      <div className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-2">
        <p
          className="text-xs text-muted-foreground"
          data-testid="last-refreshed-timestamp"
        >
          {lastRefreshedAt ? (
            <>
              Last refreshed:{" "}
              {/* Intl/ICU can format this timestamp slightly differently in
                  Node (SSR) vs the browser, tripping a React hydration text
                  mismatch (#418/#425). The value is intentionally client-
                  authoritative (manual refresh stamps new Date()), so we
                  suppress the warning on just this node. */}
              <span className="font-mono" suppressHydrationWarning>
                {formatMyt(lastRefreshedAt)}
              </span>
            </>
          ) : (
            "Not yet refreshed."
          )}
        </p>
        <button
          onClick={() => void doFetch()}
          disabled={loading}
          className="rounded-md border border-border px-3 py-1 text-xs font-medium text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? "Refreshing…" : "Refresh now"}
        </button>
      </div>

      {/* Error banner (non-fatal — last snapshot stays rendered) */}
      {error && (
        <div className="rounded-md bg-destructive/10 px-4 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      {/* Section 1 — Infrastructure: core datastores up/down + latency */}
      <section className="space-y-4">
        <h2 className="text-sm font-semibold text-foreground">
          Infrastructure
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <SupabaseStatusTile data={snapshot?.supabase ?? null} />
          <RedisStatusTile data={snapshot?.redis ?? null} />
        </div>
      </section>

      {/* Section 2 — Service & Channel Connectivity: external integrations reachable? */}
      <section className="space-y-4">
        <h2 className="text-sm font-semibold text-foreground">
          Service &amp; Channel Connectivity
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <ServiceConnectivityTile
            title="Chatwoot"
            data={snapshot?.chatwoot ?? null}
          />
          <ServiceConnectivityTile title="EA" data={snapshot?.ea ?? null} />
          <MetaRateLimitTile items={snapshot?.meta_rate_limits ?? []} />
          <WebhookHealthTile
            title="Telegram Webhooks"
            data={snapshot?.telegram_webhooks ?? null}
          />
          <WebhookHealthTile
            title="Meta Webhooks"
            data={snapshot?.meta_webhooks ?? null}
          />
        </div>
      </section>

      {/* Section 3 — Traffic & Engagement: what's flowing through the platform */}
      <section className="space-y-4">
        <h2 className="text-sm font-semibold text-foreground">
          Traffic &amp; Engagement
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <ActiveTenantsTile data={snapshot?.active_tenants ?? null} />
          <MessagingVolumeTile data={snapshot?.messaging ?? null} />
          <EmailDraftQueueTile data={snapshot?.email_drafts ?? null} />
          <KnowledgeGapCapturesTile data={snapshot?.knowledge_gaps ?? null} />
        </div>
      </section>

      {/* Section 4 — Spend & Quota: cost + tenant cap saturation */}
      <section className="space-y-4">
        <h2 className="text-sm font-semibold text-foreground">
          Spend &amp; Quota
        </h2>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div>
            <LlmTokenBurnTile data={snapshot?.llm ?? null} />
          </div>
          <div className="lg:col-span-2">
            <QuotaSaturationTile data={snapshot?.quota ?? null} />
          </div>
        </div>
      </section>

      {/* Section 5 — Queues & Failures: ops health and things going wrong */}
      <section className="space-y-4">
        <h2 className="text-sm font-semibold text-foreground">
          Queues &amp; Failures
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <BroadcastQueueDepthTile data={snapshot?.broadcasts ?? null} />
          <RecentFailuresTile data={snapshot?.failures ?? null} />
          <ApmErrorsTile data={snapshot?.apm ?? null} />
          <WebhookEventErrorsTile
            data={snapshot?.webhook_event_errors ?? null}
          />
        </div>
      </section>
    </div>
  );
}
