"use client";

/**
 * Cost-cutting revisit banner (Phase 9 PR-10).
 *
 * Shown when data-window age >= 4 weeks of GA data has accumulated.
 * Text: "4 weeks of data accumulated — review max_tokens settings"
 */

import { Card } from "@/components/ui/card";

interface Props {
  /** ISO-8601 date string for the earliest data point (period start). */
  dataWindowStart: string | null | undefined;
}

function weeksElapsed(from: string): number {
  const start = new Date(from);
  const now = new Date();
  const ms = now.getTime() - start.getTime();
  return ms / (1000 * 60 * 60 * 24 * 7);
}

export function CostCuttingRevisitBanner({ dataWindowStart }: Props) {
  if (!dataWindowStart) return null;

  const weeks = weeksElapsed(dataWindowStart);
  if (weeks < 4) return null;

  return (
    <Card className="border-amber-300 bg-amber-50 px-4 py-3">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 text-amber-600" aria-hidden>
          &#9888;
        </div>
        <div>
          <p className="text-sm font-medium text-amber-800">
            4 weeks of data accumulated — review max_tokens settings
          </p>
          <p className="mt-0.5 text-xs text-amber-700">
            Enough data has been collected to evaluate whether the current
            max_tokens limits are optimal. Check the cache hit rate: if it is
            consistently below 30%, the system prompt may be churning or the
            cache TTL is too short (see ADR-031).
          </p>
        </div>
      </div>
    </Card>
  );
}
