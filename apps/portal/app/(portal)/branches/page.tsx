/**
 * Branches list — Screen #12 (Phase 8 PR-9).
 *
 * Pro+ tier-gated per ADR-041 Amendment. Starter tenants see a
 * read-only "HQ" placeholder with an upgrade CTA; Pro+ tenants get
 * full CRUD via the BranchesClient.
 */

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { BranchesClient } from "@/components/branches/BranchesClient";
import { BranchesStarterPlaceholder } from "@/components/branches/BranchesStarterPlaceholder";
import { TierGuardWrapper } from "@/components/branches/TierGuardWrapper";

interface BranchDTO {
  id: string;
  tenant_id: string;
  name: string;
  created_at: string;
}

async function loadBranches(): Promise<{
  items: BranchDTO[];
  error?: string;
}> {
  try {
    const res = await apiFetchServer<BranchDTO[]>(TenantRoutes.branches());
    return { items: res ?? [] };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /branches] fetch failed", err);
    return {
      items: [],
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load branches.",
    };
  }
}

export default async function BranchesPage() {
  const { items, error } = await loadBranches();

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">Branches</h1>
        <p className="text-sm text-muted-foreground">
          Multiple physical locations or sub-teams? Tag content, audiences, and
          bookings per branch so the bot can route accordingly.
        </p>
      </header>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      <TierGuardWrapper
        proContent={<BranchesClient initialBranches={items} />}
        starterContent={<BranchesStarterPlaceholder />}
      />
    </div>
  );
}
