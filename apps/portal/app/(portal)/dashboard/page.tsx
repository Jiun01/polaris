/**
 * Dashboard screen #1 (Phase 8 PR-3).
 *
 * Server Component. Fetches the aggregate summary in one round-trip
 * via `lib/api/server.ts`, then renders the widgets in `components/dashboard/widgets.tsx`.
 *
 * Layout (per portal_design_prompt.md §1):
 *   - Suspension banner (top, only when active)
 *   - Quota dial (full-width primary)
 *   - 3 numeric tiles (broadcasts / promotions / ingestion) in a row
 *   - Recent conversations (left, 2/3 wide) + Reply sparkline (right, 1/3 wide)
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import {
  ActiveBroadcastsCount,
  ActivePromotionsCount,
  type DashboardSummaryDTO,
  IngestionInFlight,
  QuotaDial,
  QuotaSparkline,
  RecentConversations,
  SuspensionBanner,
} from "@/components/dashboard/widgets";
import { type ChannelKind } from "@/lib/channel-kinds";

const ONBOARDING_SKIP_COOKIE = "polaris_onboarding_skipped";

// Legacy "instagram" included: /api/tenant/channels' per_kind is a
// dict[ChannelKind, int] seeded via dict.fromkeys(get_args(ChannelKind), 0)
// in tenant_channels.py, where ChannelKind is the 6-value Literal (5
// canonical + legacy "instagram") — the real response always has all 6
// keys. Previously typed as a stale 3-key shape ({whatsapp, instagram,
// messenger}); the extra keys were silently dropped by TS's structural
// typing (no runtime error, just under-typed) — fixed per plan Decision #6.
interface ChannelsCountResponse {
  per_kind: Record<ChannelKind | "instagram", number>;
}

/**
 * Fresh-tenant redirect: zero channels connected AND no explicit skip
 * cookie → redirect to /onboarding. Set by the wizard's Skip button
 * with a 30-day expiry. We check channels rather than other signals
 * so a brand-new tenant with no inbound traffic still gets the wizard,
 * but a tenant who skipped intentionally sees the Dashboard next time.
 *
 * Errors fetching channels are treated as 'do not redirect' so a
 * momentary API hiccup doesn't bounce the tenant away from /dashboard
 * they were trying to view.
 */
async function shouldRedirectToOnboarding(): Promise<boolean> {
  const jar = cookies();
  if (jar.get(ONBOARDING_SKIP_COOKIE)?.value === "1") return false;
  try {
    const res = await apiFetchServer<ChannelsCountResponse>(
      TenantRoutes.channels(),
    );
    if (res === undefined) return false;
    // Sum ALL kinds, not just whatsapp/instagram/messenger (the previous
    // 3-key sum under-counted per Decision #6 — a tenant with ONLY
    // telegram/email/instagram_login connected would incorrectly compute
    // total=0 and get redirected back into onboarding on every visit).
    const total = Object.values(res.per_kind ?? {}).reduce(
      (sum, n) => sum + (n ?? 0),
      0,
    );
    return total === 0;
  } catch {
    return false;
  }
}

const FALLBACK_SUMMARY: DashboardSummaryDTO = {
  plan_tier: "starter",
  quota: {
    bot_replies_used: 0,
    bot_replies_cap: 0,
    knowledge_chunks_used_mb: 0,
    knowledge_chunks_cap_mb: 0,
    broadcasts_sent_this_period: 0,
    bookings_made_this_period: 0,
  },
  recent_conversations: [],
  active_broadcasts_count: 0,
  active_promotions_count: 0,
  ingestion_jobs_in_flight: 0,
  quota_replies_7d_sparkline: [0, 0, 0, 0, 0, 0, 0],
  suspension_state: null,
};

async function loadSummary(): Promise<{
  summary: DashboardSummaryDTO;
  error?: string;
}> {
  try {
    const summary = await apiFetchServer<DashboardSummaryDTO>(
      TenantRoutes.dashboardSummary(),
    );
    if (summary === undefined) {
      // /summary should never 204 (it always returns a body). Defensive
      // fallback in case the upstream changes shape unexpectedly.
      return {
        summary: FALLBACK_SUMMARY,
        error: "Empty dashboard response from API.",
      };
    }
    return { summary };
  } catch (err) {
    // Don't crash the page; the Dashboard is the landing screen and a
    // hard 500 here means the tenant has no recourse but a refresh.
    // We log + render the fallback so the shell stays usable.
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal /dashboard] summary fetch failed: ${code}`, err);
    return {
      summary: FALLBACK_SUMMARY,
      error:
        "We're having trouble loading your dashboard. Counts may be stale.",
    };
  }
}

export default async function DashboardPage() {
  if (await shouldRedirectToOnboarding()) {
    redirect("/onboarding");
  }
  const { summary, error } = await loadSummary();

  return (
    <div className="space-y-6">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Dashboard</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            A quick read on your account&apos;s health.
          </p>
        </div>
      </header>

      {/* suspension_state is `Field(None, ...)` in Pydantic — nullable AND
          defaulted, so the generated type also marks it optional (`?:`);
          the router always sets it explicitly, so it's never actually
          undefined on the wire. Normalize for SuspensionBanner's stricter
          prop type. */}
      <SuspensionBanner state={summary.suspension_state ?? null} />

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      <QuotaDial quota={summary.quota} />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <ActiveBroadcastsCount value={summary.active_broadcasts_count} />
        <ActivePromotionsCount value={summary.active_promotions_count} />
        <IngestionInFlight value={summary.ingestion_jobs_in_flight} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <RecentConversations rows={summary.recent_conversations} />
        </div>
        <QuotaSparkline values={summary.quota_replies_7d_sparkline} />
      </div>
    </div>
  );
}
