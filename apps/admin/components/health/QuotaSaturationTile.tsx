/**
 * Quota saturation tile (System Health enrichment 2026-06-01).
 *
 * Tenants near (>=80%) or over (>=100%) their monthly bot-reply cap, with the
 * most-saturated tenants listed. Source: tenant_usage vs tenant_quotas.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt } from "@/lib/format";
import type {
  QuotaSaturation,
  QuotaSaturationTenant,
} from "@/lib/types/health";

interface Props {
  data: QuotaSaturation | null;
}

function pctClass(pct: number | null): string {
  if (pct === null) return "text-muted-foreground";
  if (pct >= 100) return "text-destructive";
  if (pct >= 80) return "text-amber-600 dark:text-amber-400";
  return "text-green-600 dark:text-green-400";
}

function Row({ t }: { t: QuotaSaturationTenant }) {
  // usage_pct/cap are `X | None = Field(None, ...)` — nullable + defaulted,
  // so the generated type also marks them optional (`?:`) on top of
  // `X | null`; always present on the wire, normalize undefined to null.
  const pct = t.usage_pct ?? null;
  const cap = t.cap ?? null;
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="truncate" title={t.tenant_id}>
        {t.tenant_name ?? t.tenant_id.slice(0, 8)}
        {t.plan_tier ? (
          <span className="ml-1 text-muted-foreground/70">({t.plan_tier})</span>
        ) : null}
      </span>
      <span className={`shrink-0 tabular-nums font-medium ${pctClass(pct)}`}>
        {pct === null ? "—" : `${pct.toFixed(0)}%`}
        <span className="ml-1 font-normal text-muted-foreground/60">
          {formatInt(t.bot_replies)}
          {cap ? `/${formatInt(cap)}` : ""}
        </span>
      </span>
    </div>
  );
}

export function QuotaSaturationTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Quota Saturation
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">Unavailable</p>
        </CardContent>
      </Card>
    );
  }

  // `top` is `Field(default_factory=list)` — always present on the wire,
  // but marked optional (`?:`) in the generated type since it has a
  // Pydantic default. Same for each row's usage_pct (nullable + defaulted).
  const meaningful = (data.top ?? []).filter(
    (t) => t.usage_pct != null && t.usage_pct > 0,
  );

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          Quota Saturation
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="mb-3 flex gap-4">
          <div className="flex flex-col gap-0.5">
            <span
              className={`text-xl font-bold tabular-nums ${data.over_cap_count > 0 ? "text-destructive" : ""}`}
            >
              {formatInt(data.over_cap_count)}
            </span>
            <span className="text-xs text-muted-foreground">Over cap</span>
          </div>
          <div className="flex flex-col gap-0.5">
            <span
              className={`text-xl font-bold tabular-nums ${data.near_cap_count > 0 ? "text-amber-600 dark:text-amber-400" : ""}`}
            >
              {formatInt(data.near_cap_count)}
            </span>
            <span className="text-xs text-muted-foreground">
              Near cap (≥80%)
            </span>
          </div>
        </div>
        {meaningful.length > 0 ? (
          <div className="space-y-1.5 border-t border-border pt-2">
            {meaningful.map((t) => (
              <Row key={t.tenant_id} t={t} />
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            All tenants comfortably under cap.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
