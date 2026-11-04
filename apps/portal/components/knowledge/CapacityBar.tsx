/**
 * Knowledge capacity bar (Phase 8 PR-6).
 *
 * Renders the tenant's KB usage as a progress bar against tier cap.
 * Uses the same percent-based color band as the dashboard QuotaDial
 * (amber at 80%, red at 100%) so the operator's mental model carries
 * across surfaces.
 *
 * Pure presentational — values come from the parent's API fetch.
 */

import { cn } from "@/lib/utils";
import { formatPercent } from "@/lib/format";

export function CapacityBar({
  usedMb,
  capMb,
  availableMb,
}: {
  usedMb: number;
  capMb: number;
  availableMb: number;
}) {
  const pct = capMb > 0 ? Math.min(100, (usedMb / capMb) * 100) : 0;

  // Color band mirrors lib/format.ts quotaDialColor() — kept inline here
  // because the bar uses background fills, not a ring, and reusing the
  // ring/text helper would require an awkward shim.
  let fillClass = "bg-foreground";
  if (pct >= 100) {
    fillClass = "bg-destructive";
  } else if (pct >= 80) {
    fillClass = "bg-warning";
  }

  return (
    <div className="w-full max-w-md">
      <div className="flex items-baseline justify-between text-xs text-muted-foreground">
        <span>Knowledge storage</span>
        <span className="tabular-nums">
          {usedMb} MB / {capMb} MB ·{" "}
          <span className={cn(pct >= 80 && "text-foreground font-medium")}>
            {formatPercent(usedMb, capMb)}
          </span>
        </span>
      </div>
      <div
        className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Knowledge storage used"
      >
        <div
          className={cn("h-full rounded-full transition-all", fillClass)}
          style={{ width: `${pct}%` }}
        />
      </div>
      {availableMb <= 0 && capMb > 0 ? (
        <p className="mt-1.5 text-xs text-destructive">
          Storage full. Delete unused sources or upgrade your plan to add more.
        </p>
      ) : null}
    </div>
  );
}
