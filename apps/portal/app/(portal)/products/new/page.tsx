/**
 * Product create page (Phase 15 PR-7).
 *
 * Server Component shell hands off to ProductForm. On create we redirect to the
 * edit page so the tenant can add photos immediately.
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { TenantRoutes } from "@/lib/api/routes";
import { ProductForm } from "@/components/products/ProductForm";

interface BranchDTO {
  id: string;
  name: string;
}

async function loadBranches(): Promise<BranchDTO[]> {
  try {
    const res = await apiFetchServer<BranchDTO[]>(TenantRoutes.branches());
    return res ?? [];
  } catch {
    // Branches are Pro+ — Starter tenants 200 with an empty list.
    return [];
  }
}

export default async function NewProductPage() {
  const branches = await loadBranches();
  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/products"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All products
        </Link>
      </div>

      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">New product</h1>
        <p className="text-sm text-muted-foreground">
          Add the details now; you&apos;ll add photos on the next screen.
        </p>
      </header>

      <ProductForm mode="create" branches={branches} />
    </div>
  );
}
