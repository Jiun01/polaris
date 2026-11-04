/**
 * Knowledge gap captures tile (System Health dashboard 2026-06-09).
 *
 * 24 h gap-capture totals (no_retrieval, low_retrieval, handoff) plus
 * the aggregate total, sourced from knowledge_gap_misses via the health
 * enrichment query. Ultra tenants only.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt, formatMyt } from "@/lib/format";
import type { KnowledgeGapCaptures } from "@/lib/types/health";

interface Props {
  data: KnowledgeGapCaptures | null;
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xl font-bold tabular-nums">{formatInt(value)}</span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  );
}

export function KnowledgeGapCapturesTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Knowledge Gaps (24h)
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
          Knowledge Gaps (24h)
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="mb-4">
          <span className="text-3xl font-bold tabular-nums">
            {formatInt(data.total_24h)}
          </span>
          <span className="ml-2 text-sm text-muted-foreground">
            total (24 h)
          </span>
        </div>
        <div className="grid grid-cols-3 gap-4">
          <Metric label="No retrieval" value={data.no_retrieval_24h} />
          <Metric label="Low retrieval" value={data.low_retrieval_24h} />
          <Metric label="Handoff" value={data.handoff_24h} />
        </div>
        {data.latest_at && (
          <p className="mt-3 text-xs text-muted-foreground/60">
            Latest: {formatMyt(data.latest_at)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
