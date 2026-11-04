/**
 * Meta API rate-limit headroom tile (Phase 9 PR-6).
 *
 * Per-surface (WA / IG / Messenger) headroom percentage, sourced from
 * last 24 h Meta header captures stored in meta_api_usage. Low headroom
 * (<20%) is highlighted in red; medium (<50%) in amber.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMyt } from "@/lib/format";
import type { MetaRateLimitHeadroom } from "@/lib/types/health";

interface Props {
  items: MetaRateLimitHeadroom[];
}

const SURFACE_LABELS: Record<string, string> = {
  whatsapp: "WhatsApp",
  instagram: "Instagram",
  messenger: "Messenger",
};

function headroomClass(pct: number): string {
  if (pct < 20) return "text-destructive";
  if (pct < 50) return "text-amber-600 dark:text-amber-400";
  return "text-green-600 dark:text-green-400";
}

function HeadroomBar({ pct }: { pct: number }) {
  const clampedPct = Math.max(0, Math.min(100, pct));
  const barClass =
    clampedPct < 20
      ? "bg-destructive"
      : clampedPct < 50
        ? "bg-amber-500"
        : "bg-green-500";

  return (
    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div
        className={`h-full rounded-full transition-all ${barClass}`}
        style={{ width: `${clampedPct}%` }}
      />
    </div>
  );
}

export function MetaRateLimitTile({ items }: Props) {
  if (items.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Meta Rate Limits
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">
            No usage data in the last 24 h.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          Meta Rate Limits
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {items.map((item) => (
          <div key={item.surface}>
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-medium">
                {SURFACE_LABELS[item.surface] ?? item.surface}
              </span>
              <span
                className={`text-lg font-bold tabular-nums ${headroomClass(item.calls_remaining_pct)}`}
              >
                {item.calls_remaining_pct.toFixed(1)}
                <span className="text-xs font-normal text-muted-foreground">
                  %
                </span>
              </span>
            </div>
            <HeadroomBar pct={item.calls_remaining_pct} />
            <p className="mt-0.5 text-right text-xs text-muted-foreground/60">
              {formatMyt(item.last_observed_at)}
            </p>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
