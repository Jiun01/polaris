/**
 * Knowledge — Sources page.
 *
 * Server Component. Fetches the tenant's knowledge sources + capacity bar
 * in parallel from the Polaris API, then renders the Sources table +
 * a client-side "Add knowledge" button that opens a dialog
 * (URL / text / file submission paths).
 *
 * Sources come from the declarative /sources endpoint (desired-state ledger).
 * The operator's local sync worker reconciles these into knowledge_chunks;
 * the portal is only responsible for managing the desired-state rows.
 *
 * CapacityBar uses /capacity, which is unchanged.
 */

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { AddKnowledgeButton } from "@/components/knowledge/AddKnowledgeButton";
import { CapacityBar } from "@/components/knowledge/CapacityBar";
import {
  KnowledgeSourcesTable,
  type KnowledgeSource,
} from "@/components/knowledge/KnowledgeSourcesTable";

interface CapacityResponseDTO {
  used_mb: number;
  cap_mb: number;
  available_mb: number;
}

async function loadKnowledge(): Promise<{
  sources: KnowledgeSource[];
  capacity: CapacityResponseDTO;
  error?: string;
}> {
  // Parallel fetches — independent lookups, no shared transaction needed.
  const [sourcesRes, capacityRes] = await Promise.allSettled([
    apiFetchServer<KnowledgeSource[]>(TenantRoutes.knowledgeSources()),
    apiFetchServer<CapacityResponseDTO>(TenantRoutes.knowledgeCapacity()),
  ]);

  const capacity =
    capacityRes.status === "fulfilled"
      ? (capacityRes.value ?? { used_mb: 0, cap_mb: 0, available_mb: 0 })
      : { used_mb: 0, cap_mb: 0, available_mb: 0 };

  if (sourcesRes.status === "rejected") {
    const err = sourcesRes.reason;
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal /knowledge] sources fetch failed: ${code}`, err);
    return {
      sources: [],
      capacity,
      error: "We're having trouble loading your knowledge sources.",
    };
  }

  return {
    sources: sourcesRes.value ?? [],
    capacity,
  };
}

export default async function KnowledgePage() {
  const { sources, capacity, error } = await loadKnowledge();

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <CapacityBar
          usedMb={capacity.used_mb}
          capMb={capacity.cap_mb}
          availableMb={capacity.available_mb}
        />
        <AddKnowledgeButton />
      </div>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      {sources.length === 0 && !error ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No sources yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Add a website URL, paste text, or upload a file to teach your bot.
            Your bot will use this after the next knowledge sync.
          </p>
          <div className="mt-4 inline-block">
            <AddKnowledgeButton />
          </div>
        </div>
      ) : (
        <KnowledgeSourcesTable sources={sources} />
      )}
    </div>
  );
}
