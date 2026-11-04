/**
 * Email draft queue tile (System Health dashboard 2026-06-09).
 *
 * Pending drafts headline + 24 h accepted/edited/ignored breakdown,
 * sourced from email_drafts table via the health enrichment query.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt, formatMyt } from "@/lib/format";
import type { EmailDraftQueue } from "@/lib/types/health";

interface Props {
  data: EmailDraftQueue | null;
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xl font-bold tabular-nums">{formatInt(value)}</span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  );
}

export function EmailDraftQueueTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Email Draft Queue
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
          Email Draft Queue
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="mb-4">
          <span className="text-3xl font-bold tabular-nums">
            {formatInt(data.pending)}
          </span>
          <span className="ml-2 text-sm text-muted-foreground">pending</span>
        </div>
        <div className="grid grid-cols-3 gap-4">
          <Metric label="Accepted (24 h)" value={data.accepted_24h} />
          <Metric label="Edited (24 h)" value={data.edited_24h} />
          <Metric label="Ignored (24 h)" value={data.ignored_24h} />
        </div>
        {data.latest_draft_at && (
          <p className="mt-3 text-xs text-muted-foreground/60">
            Latest draft: {formatMyt(data.latest_draft_at)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
