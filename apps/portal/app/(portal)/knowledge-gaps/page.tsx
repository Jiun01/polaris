/**
 * Knowledge Gaps — Ultra-only screen (Knowledge Gap Detection M3).
 *
 * Surfaces the questions the bot couldn't confidently answer, grouped into
 * ranked themes. Ultra+ per the feature gate; lower tiers see an upgrade card
 * (we never hide the screen — visibility teaches the upgrade path).
 *
 * Task 5: reads ?window=7|30|90 from searchParams, passes to fetch + renders
 * a 7/30/90 window selector as plain links (URL is the source of truth so
 * refresh / share preserve the chosen window — mirror of conversations/page).
 */

import Link from "next/link";

import { PolarisApiError } from "@/lib/api/errors";
import { apiFetchServer } from "@/lib/api/server";
import { TenantRoutes } from "@/lib/api/routes";
import { KnowledgeGapsTierGuard } from "@/components/knowledge-gaps/KnowledgeGapsTierGuard";
import {
  KnowledgeGapsContent,
  type KnowledgeGapsSummaryDTO,
} from "@/components/knowledge-gaps/KnowledgeGapsContent";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Window param helpers
// ---------------------------------------------------------------------------

const ALLOWED_WINDOWS = [7, 30, 90] as const;
type WindowDays = (typeof ALLOWED_WINDOWS)[number];

function parseWindow(raw: string | string[] | undefined): WindowDays {
  if (typeof raw !== "string") return 30;
  const n = parseInt(raw, 10);
  return (ALLOWED_WINDOWS as readonly number[]).includes(n)
    ? (n as WindowDays)
    : 30;
}

// ---------------------------------------------------------------------------
// Data fetching
// ---------------------------------------------------------------------------

const EMPTY_SUMMARY: KnowledgeGapsSummaryDTO = {
  plan_tier: "starter",
  window_days: 30,
  total_misses: 0,
  themes: [],
};

async function loadGaps(windowDays: WindowDays): Promise<{
  summary: KnowledgeGapsSummaryDTO;
  error?: string;
}> {
  try {
    const summary = await apiFetchServer<KnowledgeGapsSummaryDTO>(
      TenantRoutes.knowledgeGapsSummary(windowDays),
    );
    return { summary: summary ?? EMPTY_SUMMARY };
  } catch (err) {
    // 403 = tenant below Ultra. Expected — the tier guard renders the upgrade
    // card, so swallow it (no error banner). Anything else is a real failure.
    if (err instanceof PolarisApiError && err.status === 403) {
      return { summary: EMPTY_SUMMARY };
    }
    // Full detail (incl. any PolarisApiError message/code) stays in the server
    // log; the UI shows a single generic string so internal error details
    // (e.g. service_suspended:payment_overdue) never reach the banner.
    // eslint-disable-next-line no-console
    console.error("[portal /knowledge-gaps] fetch failed", err);
    return { summary: EMPTY_SUMMARY, error: "Couldn't load knowledge gaps." };
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function KnowledgeGapsPage({
  searchParams,
}: {
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const windowDays = parseWindow(searchParams.window);
  const { summary, error } = await loadGaps(windowDays);

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">
          Knowledge Gaps
        </h1>
        <p className="text-sm text-muted-foreground">
          The questions your bot couldn&apos;t confidently answer, grouped and
          ranked, so you know exactly what to add to your knowledge base.
        </p>
      </header>

      {/* Window selector — plain links; URL is the source of truth */}
      <nav className="flex items-center gap-2" aria-label="Look-back window">
        {ALLOWED_WINDOWS.map((w) => {
          const active = w === windowDays;
          return (
            <Link
              key={w}
              href={`/knowledge-gaps?window=${w}`}
              aria-pressed={active}
              className={cn(
                "rounded-full border px-3 py-1 text-sm transition-colors",
                active
                  ? "border-foreground bg-foreground text-background"
                  : "border-border bg-card text-muted-foreground hover:bg-accent/50 hover:text-foreground",
              )}
            >
              {w}d
            </Link>
          );
        })}
      </nav>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      <KnowledgeGapsTierGuard>
        <KnowledgeGapsContent summary={summary} />
      </KnowledgeGapsTierGuard>
    </div>
  );
}
