"use client";

/**
 * Per-intent-tier breakdown table (Phase 9 PR-10).
 *
 * 3 rows: simple / medium / hard.
 * Columns: model_used, avg_input_tokens, avg_output_tokens,
 *          avg_cache_read_tokens, cache_hit_rate, sample_count.
 * Includes a simple cap-vs-actual gauge for cache hit rate.
 */

import { Card } from "@/components/ui/card";
import { formatInt } from "@/lib/format";
import type { PerIntentTierBreakdown } from "@/lib/types/billing";

const TIER_ORDER = ["simple", "medium", "hard"];

function tierLabel(tier: string): string {
  switch (tier) {
    case "simple":
      return "Haiku — Simple";
    case "medium":
      return "Haiku — Medium";
    case "hard":
      return "Sonnet — Hard";
    default:
      return tier;
  }
}

function CacheGauge({ rate }: { rate: number }) {
  const pct = Math.min(100, Math.round(rate * 100));
  const color =
    pct >= 30 ? "bg-success" : pct >= 10 ? "bg-warning" : "bg-destructive";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 rounded-full bg-muted overflow-hidden">
        <div
          className={`h-full rounded-full ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs tabular-nums">{pct}%</span>
    </div>
  );
}

interface Props {
  rows: PerIntentTierBreakdown[];
}

export function IntentTierBreakdown({ rows }: Props) {
  if (rows.length === 0) {
    return (
      <Card className="border-dashed p-6 text-sm text-muted-foreground">
        No per-intent-tier data — Phase 4 call-sites must emit OTel labels.
      </Card>
    );
  }

  // Sort by canonical tier order
  const sorted = [...rows].sort((a, b) => {
    const ai = TIER_ORDER.indexOf(a.intent_tier);
    const bi = TIER_ORDER.indexOf(b.intent_tier);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
            <tr>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Tier / Model
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Avg Input Tokens
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Avg Output Tokens
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Avg Cache Read
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Cache Hit Rate
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Samples
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr
                key={`${row.intent_tier}-${row.model_used}`}
                className="border-t border-border hover:bg-muted/40 transition"
              >
                <td className="px-4 py-2.5 font-medium text-xs">
                  {tierLabel(row.intent_tier)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                  {formatInt(row.avg_input_tokens)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                  {formatInt(row.avg_output_tokens)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                  {formatInt(row.avg_cache_read_tokens)}
                </td>
                <td className="px-4 py-2.5">
                  <CacheGauge rate={row.cache_hit_rate} />
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs text-muted-foreground">
                  {formatInt(row.sample_count)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
