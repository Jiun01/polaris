/**
 * LLM token burn tile (System Health enrichment 2026-06-01).
 *
 * Token throughput from the messages table (24 h + month-to-date) with a
 * LOCAL estimated MYR figure. The Cost dashboard (Grafana Mimir) remains the
 * source of record for billed spend — hence the "est." label + link.
 */

import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt, formatMyr } from "@/lib/format";
import type { LlmTokenBurn } from "@/lib/types/health";

interface Props {
  data: LlmTokenBurn | null;
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return formatInt(n);
}

export function LlmTokenBurnTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            LLM Token Burn
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">Unavailable</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          LLM Token Burn
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-baseline gap-1">
          <span className="text-xl font-bold tabular-nums">
            {/* est_cost_myr_mtd is `float | None = Field(None, ...)` —
                nullable + defaulted, so the generated type also marks it
                optional (`?:`); always present on the wire. */}
            {data.est_cost_myr_mtd == null
              ? "—"
              : formatMyr(data.est_cost_myr_mtd)}
          </span>
          <span className="text-xs text-muted-foreground">est. MTD</span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <span className="text-muted-foreground">In (24 h)</span>
          <span className="text-right tabular-nums">
            {compact(data.input_tokens_24h)}
          </span>
          <span className="text-muted-foreground">Out (24 h)</span>
          <span className="text-right tabular-nums">
            {compact(data.output_tokens_24h)}
          </span>
          <span className="text-muted-foreground">In (MTD)</span>
          <span className="text-right tabular-nums">
            {compact(data.input_tokens_mtd)}
          </span>
          <span className="text-muted-foreground">Out (MTD)</span>
          <span className="text-right tabular-nums">
            {compact(data.output_tokens_mtd)}
          </span>
        </div>
        <Link
          href="/cost"
          className="mt-3 inline-block text-xs text-primary underline-offset-2 hover:underline"
        >
          Billed cost (Cost dashboard) →
        </Link>
      </CardContent>
    </Card>
  );
}
