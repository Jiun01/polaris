"use client";

/**
 * Billing month filter (Phase 9 PR-7; repurposed).
 *
 * Originally built to download a monthly invoice-index PDF, but that backend
 * was never implemented — GET /api/admin/billing/archive returns invoice
 * COUNTS grouped by (year_month, tenant_id), not a downloadable index. This
 * component now consumes that real shape: it aggregates the rows into the set
 * of months that have invoices and renders a "jump to month" filter that
 * drives the ?year_month= query param on the billing list.
 *
 * Lives inside the page's <Suspense> boundary (required by useSearchParams).
 */

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

// Mirrors the API's _ArchiveResponse (per year_month × tenant counts).
interface ArchiveRow {
  year_month: string; // e.g. "2026-06"
  tenant_id: string;
  count: number;
}
interface ArchiveResponse {
  items: ArchiveRow[];
}

interface MonthOption {
  month: string; // "2026-06"
  label: string; // "June 2026"
  count: number; // total invoices that month (across tenants)
}

function monthLabel(ym: string): string {
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m) return ym;
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-MY", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function ArchiveMonthPicker() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const current = searchParams.get("year_month") ?? "";

  const [months, setMonths] = useState<MonthOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    adminFetch<ArchiveResponse>(adminRoutes.billing.archive())
      .then((res) => {
        if (cancelled) return;
        const byMonth = new Map<string, number>();
        for (const row of res?.items ?? []) {
          byMonth.set(
            row.year_month,
            (byMonth.get(row.year_month) ?? 0) + row.count,
          );
        }
        const list: MonthOption[] = [...byMonth.entries()]
          .map(([month, count]) => ({ month, label: monthLabel(month), count }))
          .sort((a, b) => b.month.localeCompare(a.month));
        setMonths(list);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(
          err instanceof ApiError ? err.message : "Failed to load months",
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function navigate(ym: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (ym) params.set("year_month", ym);
    else params.delete("year_month");
    const qs = params.toString();
    router.push(qs ? `/billing?${qs}` : "/billing");
  }

  if (loading) {
    return (
      <span className="text-xs text-muted-foreground">Loading months…</span>
    );
  }
  if (error) {
    return <span className="text-xs text-destructive">Months unavailable</span>;
  }
  if (months.length === 0) {
    return (
      <span className="text-xs text-muted-foreground">No invoices yet.</span>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <Label htmlFor="archive-month" className="text-sm whitespace-nowrap">
        Month:
      </Label>
      <select
        id="archive-month"
        value={current}
        onChange={(e) => navigate(e.target.value)}
        className="flex h-8 rounded-md border border-input bg-transparent px-2 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <option value="">All months</option>
        {months.map((m) => (
          <option key={m.month} value={m.month}>
            {m.label} ({m.count})
          </option>
        ))}
      </select>
    </div>
  );
}
