"use client";

/**
 * MRR overview card.
 *
 * Displays the current MRR big number, a 30-day trend table, and a manual
 * recompute button.
 */

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { adminRoutes } from "@/lib/api/routes";
import { adminFetch } from "@/lib/api/client";

export interface MrrSnapshot {
  mrr_myr: number;
  alert_band: "green" | "amber" | "red";
  computed_at: string;
}

export interface MrrRollupData extends MrrSnapshot {
  trend_30d: MrrSnapshot[];
}

interface Props {
  initial: MrrRollupData;
}

// Built once at module load, not per call. Intentionally ms-MY / 2-dp —
// distinct from lib/format.ts (en-MY); do NOT swap for the shared formatMyr,
// it would change the displayed currency.
const MRR_MYR_FORMAT = new Intl.NumberFormat("ms-MY", {
  style: "currency",
  currency: "MYR",
  maximumFractionDigits: 2,
});

function formatMyr(value: number): string {
  return MRR_MYR_FORMAT.format(value);
}

function formatTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-MY", {
      dateStyle: "short",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

export function MrrOverviewCard({ initial }: Props) {
  const [data, setData] = useState<MrrRollupData>(initial);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleRecompute() {
    setLoading(true);
    setError(null);
    try {
      // Must go through adminFetch (same-origin /api/proxy → FastAPI). A raw
      // fetch() to the API path hits the admin origin, which has no such route
      // → 404. (Same class as the earlier PDF-download 404.)
      const fresh = await adminFetch<MrrRollupData>(
        adminRoutes.financials.recomputeMrr(),
        { method: "POST" },
      );
      if (fresh) setData(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Recompute failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base font-semibold">MRR</CardTitle>
        <Button
          size="sm"
          variant="outline"
          onClick={handleRecompute}
          disabled={loading}
        >
          {loading ? "Computing…" : "Recompute now"}
        </Button>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Big number */}
        <div className="flex items-end gap-3">
          <span
            data-testid="mrr-big-number"
            className="text-4xl font-bold tabular-nums text-foreground"
          >
            {formatMyr(data.mrr_myr)}
          </span>
        </div>

        {/* Last computed */}
        <p className="text-xs text-muted-foreground">
          Last computed: {formatTs(data.computed_at)}
        </p>

        {error && <p className="text-xs text-destructive">Error: {error}</p>}

        {/* 30-day trend table */}
        {data.trend_30d.length > 0 && (
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">
              30-day trend ({data.trend_30d.length} snapshots)
            </p>
            <div className="max-h-48 overflow-y-auto rounded border text-xs">
              <table className="w-full">
                <thead className="sticky top-0 bg-muted/60">
                  <tr>
                    <th className="px-2 py-1 text-left font-medium">Date</th>
                    <th className="px-2 py-1 text-right font-medium">MRR</th>
                  </tr>
                </thead>
                <tbody>
                  {[...data.trend_30d].reverse().map((snap, i) => (
                    <tr key={i} className="border-t">
                      <td className="px-2 py-1 text-muted-foreground">
                        {formatTs(snap.computed_at)}
                      </td>
                      <td className="px-2 py-1 text-right tabular-nums">
                        {formatMyr(snap.mrr_myr)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
