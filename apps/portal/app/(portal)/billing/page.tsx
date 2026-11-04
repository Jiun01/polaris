/**
 * Billing & Usage — Screen #13 (Phase 8 PR-10).
 *
 * Server Component. Renders the tenant's current plan card, usage
 * dials, and invoices table. Invoice + usage upstream endpoints are
 * read-only proxies through Polaris API (live integration with the
 * Phase 10 admin billing surface is deferred); at GA the dashboard
 * summary supplies enough data to render the usage dials.
 *
 * Plan-change CTA routes to /billing/plan-change.
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Button } from "@/components/ui/button";
import { formatAbsolute, formatCount, formatMyr } from "@/lib/format";

interface DashboardSummary {
  quota: {
    bot_replies_used: number;
    bot_replies_cap: number;
    knowledge_chunks_used_mb: number;
    knowledge_chunks_cap_mb: number;
  };
  active_broadcasts_count: number;
  active_promotions_count: number;
  ingestion_jobs_in_flight: number;
  plan_tier?: string;
  suspension_state: string | null;
}

interface PlanChangeRequestDTO {
  id: string;
  status: "pending" | "approved" | "rejected" | "cancelled";
  current_tier: string;
  target_tier: string;
  created_at: string;
}

const TIER_PRICE_MYR: Record<string, number> = {
  starter: 1099,
  pro: 2499,
  ultra: 5499,
  enterprise: 0,
};

const TIER_LABEL: Record<string, string> = {
  starter: "Starter",
  pro: "Pro",
  ultra: "Ultra",
  enterprise: "Enterprise",
};

async function loadBilling(): Promise<{
  summary: DashboardSummary | null;
  pendingRequest: PlanChangeRequestDTO | null;
  error?: string;
}> {
  const [summaryRes, requestsRes] = await Promise.allSettled([
    apiFetchServer<DashboardSummary>(TenantRoutes.dashboardSummary()),
    apiFetchServer<PlanChangeRequestDTO[]>(TenantRoutes.planChangeRequests()),
  ]);

  const summary =
    summaryRes.status === "fulfilled" ? (summaryRes.value ?? null) : null;
  const pendingRequest =
    requestsRes.status === "fulfilled"
      ? ((requestsRes.value ?? []).find((r) => r.status === "pending") ?? null)
      : null;

  if (summaryRes.status === "rejected") {
    const err = summaryRes.reason;
    // eslint-disable-next-line no-console
    console.error("[portal /billing] summary fetch failed", err);
    return {
      summary: null,
      pendingRequest,
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load usage summary.",
    };
  }
  return { summary, pendingRequest };
}

function pct(used: number, cap: number): number {
  if (cap <= 0) return 0;
  return Math.min(100, Math.round((used / cap) * 100));
}

function dialColor(p: number): string {
  if (p >= 100) return "bg-destructive";
  if (p >= 80) return "bg-warning";
  return "bg-foreground";
}

export default async function BillingPage() {
  const { summary, pendingRequest, error } = await loadBilling();
  const tier = summary?.plan_tier ?? "starter";

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">
          Billing & Usage
        </h1>
        <p className="text-sm text-muted-foreground">
          Your plan, current usage against the tier&apos;s caps, and historical
          invoices.
        </p>
      </header>

      {summary?.suspension_state === "payment_overdue" ? (
        <div
          role="alert"
          className="rounded-lg border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          <strong>Payment overdue.</strong> Your account is locked until the
          outstanding invoice clears. See the{" "}
          <Link href="/pay" className="underline">
            payment page
          </Link>
          .
        </div>
      ) : null}

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      {pendingRequest ? (
        <div
          role="status"
          className="rounded-lg border border-blue-300 bg-blue-50 px-4 py-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-100"
        >
          <strong>Plan change pending.</strong> Your request to move from{" "}
          {TIER_LABEL[pendingRequest.current_tier]} to{" "}
          {TIER_LABEL[pendingRequest.target_tier]} is awaiting approval (
          {formatAbsolute(pendingRequest.created_at)}).
        </div>
      ) : null}

      <section className="rounded-xl border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-xs uppercase text-muted-foreground">
              Current plan
            </div>
            <h2 className="mt-1 text-2xl font-semibold">
              {TIER_LABEL[tier] ?? tier}
              {TIER_PRICE_MYR[tier] > 0 ? (
                <span className="ml-2 text-sm font-normal text-muted-foreground">
                  {formatMyr(TIER_PRICE_MYR[tier])} / month
                </span>
              ) : null}
            </h2>
          </div>
          <div className="flex gap-2">
            <Button asChild>
              <Link href="/billing/plan-change">Change plan</Link>
            </Button>
          </div>
        </div>
      </section>

      {summary ? (
        <section className="grid gap-4 sm:grid-cols-2">
          <UsageCard
            label="Bot replies this month"
            used={summary.quota.bot_replies_used}
            cap={summary.quota.bot_replies_cap}
            unit=""
          />
          <UsageCard
            label="Knowledge storage"
            used={summary.quota.knowledge_chunks_used_mb}
            cap={summary.quota.knowledge_chunks_cap_mb}
            unit="MB"
          />
          <SimpleStatCard
            label="Active broadcasts"
            value={summary.active_broadcasts_count}
          />
          <SimpleStatCard
            label="Active promotions"
            value={summary.active_promotions_count}
          />
        </section>
      ) : null}

      <section className="rounded-xl border bg-muted/30 p-5">
        <h2 className="text-base font-semibold">Invoices</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Invoices are issued as PDFs by the Polaris team and shared over the
          support thread.
        </p>
        <p className="mt-3 text-sm">
          Need an invoice copy? Email{" "}
          <a
            className="underline"
            href="mailto:billing@getpolaris.my?subject=Invoice%20request"
          >
            billing@getpolaris.my
          </a>
          .
        </p>
      </section>
    </div>
  );
}

function UsageCard({
  label,
  used,
  cap,
  unit,
}: {
  label: string;
  used: number;
  cap: number;
  unit: string;
}) {
  const p = pct(used, cap);
  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-2xl font-semibold tabular-nums">
          {formatCount(Math.round(used))}
          {unit ? (
            <span className="text-sm text-muted-foreground"> {unit}</span>
          ) : null}
        </span>
        <span className="text-sm text-muted-foreground">
          / {formatCount(Math.round(cap))}
          {unit ? ` ${unit}` : ""}
        </span>
      </div>
      <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={`h-full ${dialColor(p)}`}
          style={{ width: `${p}%` }}
          role="progressbar"
          aria-valuenow={p}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={label}
        />
      </div>
    </div>
  );
}

function SimpleStatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">
        {formatCount(value)}
      </div>
    </div>
  );
}
