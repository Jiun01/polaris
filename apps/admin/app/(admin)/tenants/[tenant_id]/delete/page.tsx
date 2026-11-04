/**
 * Hard-delete tenant confirmation page (Phase 9 PR-3).
 *
 * Cascade scope is fetched server-side from the tenant detail endpoint
 * so the operator sees exactly what will be archived vs deleted before
 * confirming. ADR-029 mandates the DSAR audit row and the invoice-archive
 * step; the backend admin_tenants.py implements the atomic cascade.
 */

import { notFound } from "next/navigation";

import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { DeleteConfirmation } from "@/components/tenants/DeleteConfirmation";
import type { TenantDetail } from "@/components/tenants/types";

interface CascadeScope {
  invoices_to_archive: number;
  audit_log_rows: number;
  channels: number;
  users: number;
  branches: number;
  promotions: number;
  audience_members: number;
}

interface DeletePreview {
  tenant: TenantDetail;
  cascade: CascadeScope;
}

async function fetchDeletePreview(
  tenantId: string,
): Promise<DeletePreview | null> {
  try {
    // Reuse the detail endpoint with ?preview=delete query to keep the
    // route inventory small. Backend returns the cascade scope inline.
    const res = await adminServerFetch<DeletePreview>(
      `${adminRoutes.tenants.detail(tenantId)}?preview=delete`,
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export default async function TenantDeletePage({
  params,
}: {
  params: { tenant_id: string };
}) {
  const preview = await fetchDeletePreview(params.tenant_id);
  if (!preview) notFound();
  const { tenant, cascade } = preview;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Delete tenant</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Hard-delete with DSAR cascade per ADR-029.
        </p>
      </div>
      <DeleteConfirmation
        tenantId={tenant.id}
        slug={tenant.slug}
        displayName={tenant.display_name}
        cascade={cascade}
      />
    </div>
  );
}
