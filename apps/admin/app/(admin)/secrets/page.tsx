/**
 * Secrets index (Phase 9 PR-4).
 *
 * The Secrets section is per-tenant; this page lists all active tenants
 * so the admin can navigate to the per-tenant rotation surface at
 * /secrets/{tenant_id}.
 */

import Link from "next/link";

import { Card } from "@/components/ui/card";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";

interface TenantStub {
  id: string;
  display_name: string;
  slug: string;
  status: string;
  plan_tier: string | null;
}

interface TenantsResponse {
  items: TenantStub[];
  total: number;
}

async function fetchTenants(): Promise<TenantStub[]> {
  const res = await adminServerFetch<TenantsResponse>(
    `${adminRoutes.tenants.list()}?limit=200&status=active`,
  );
  return res?.items ?? [];
}

export default async function SecretsIndexPage() {
  const tenants = await fetchTenants();

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          Secrets rotation
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Select a tenant to rotate per-service credentials (WhatsApp, Chatwoot,
          etc.). Values are encrypted with PyNaCl AEAD and never echoed back.
        </p>
      </div>

      {tenants.length === 0 ? (
        <Card className="border-dashed p-6 text-sm text-muted-foreground">
          No active tenants found.
        </Card>
      ) : (
        <Card className="divide-y divide-border">
          {tenants.map((t) => (
            <Link
              key={t.id}
              href={`/secrets/${t.id}`}
              className="flex items-center justify-between p-3 text-sm transition hover:bg-muted/50"
            >
              <div>
                <p className="font-medium">{t.display_name}</p>
                <p className="text-xs text-muted-foreground">{t.slug}</p>
              </div>
              <span className="text-xs text-muted-foreground capitalize">
                {t.plan_tier ?? "—"}
              </span>
            </Link>
          ))}
        </Card>
      )}
    </div>
  );
}
