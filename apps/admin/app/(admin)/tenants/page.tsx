/**
 * Tenants list (Phase 9 PR-3, Deliverable #1).
 *
 * The primary landing for the platform_admin. List of all tenants with
 * status, tier, MRR, last_inbound_at, user_count, and per-row actions
 * (open detail). Sortable + filterable by status; PR-3 ships a simple
 * server-side filter via searchParams, more sophisticated filtering
 * lands later if usage warrants.
 *
 * Data flow: Server Component fetches /api/admin/tenants from FastAPI
 * via lib/api/server.ts. The admin_tenants router itself lands in PR-3
 * Chunk 3 (backend) — until then the page renders an empty-state with
 * a "Create tenant" CTA, exercising the create-wizard path.
 *
 * Per the wireframe brief: dense layout, no marketing flourish, scan-
 * friendly columns. Operator efficiency > visual polish.
 */

import Link from "next/link";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { TenantsTable } from "@/components/tenants/TenantsTable";
import type { TenantSummary } from "@/components/tenants/types";

interface TenantsListResponse {
  items: TenantSummary[];
  total: number;
}

type StatusFilter = "all" | "active" | "suspended" | "deleted";

const STATUS_FILTERS: readonly StatusFilter[] = [
  "all",
  "active",
  "suspended",
  "deleted",
];

function parseFilter(raw: string | undefined): StatusFilter {
  if (!raw) return "all";
  return (STATUS_FILTERS as readonly string[]).includes(raw)
    ? (raw as StatusFilter)
    : "all";
}

async function fetchTenants(
  status: StatusFilter,
): Promise<TenantsListResponse | { error: string }> {
  try {
    const path =
      adminRoutes.tenants.list() +
      (status !== "all" ? `?status=${status}` : "");
    const res = await adminServerFetch<TenantsListResponse>(path);
    return res ?? { items: [], total: 0 };
  } catch (err) {
    if (err instanceof ApiError) {
      // PR-3 ships the UI before the backend router; render an empty
      // state with the create-tenant CTA so the wizard path is reachable.
      if (err.status === 404 || err.status === 501) {
        return { items: [], total: 0 };
      }
      return { error: err.message };
    }
    throw err;
  }
}

export default async function TenantsPage({
  searchParams,
}: {
  searchParams?: { status?: string };
}) {
  const status = parseFilter(searchParams?.status);
  const data = await fetchTenants(status);

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Tenants</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            All Polaris customer tenants. Sales-led signup per ADR-012.
          </p>
        </div>
        <Button asChild>
          <Link
            href="/tenants/new"
            className="inline-flex items-center gap-1.5"
          >
            <Plus className="h-4 w-4" aria-hidden />
            New tenant
          </Link>
        </Button>
      </div>

      <div className="flex items-center gap-1.5">
        {STATUS_FILTERS.map((f) => {
          const href = f === "all" ? "/tenants" : `/tenants?status=${f}`;
          const active = status === f;
          return (
            <Link
              key={f}
              href={href}
              className={
                active
                  ? "rounded-md bg-secondary px-2.5 py-1 text-xs font-medium capitalize text-secondary-foreground"
                  : "rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium capitalize text-muted-foreground hover:bg-muted hover:text-foreground"
              }
              aria-current={active ? "page" : undefined}
            >
              {f}
            </Link>
          );
        })}
      </div>

      {"error" in data ? (
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load tenants: {data.error}
        </Card>
      ) : (
        <TenantsTable items={data.items} />
      )}
    </div>
  );
}
