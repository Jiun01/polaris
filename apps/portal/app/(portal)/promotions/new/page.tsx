/**
 * Promotion create page (Phase 8 PR-9).
 *
 * Server Component shell hands off to PromotionForm client. New
 * promotions default to active + 30-day window so the typical
 * use case ("flash promo starting today") is one fewer click.
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { TenantRoutes } from "@/lib/api/routes";
import { PromotionForm } from "@/components/promotions/PromotionForm";

interface BranchDTO {
  id: string;
  name: string;
}

async function loadBranches(): Promise<BranchDTO[]> {
  try {
    const res = await apiFetchServer<BranchDTO[]>(TenantRoutes.branches());
    return res ?? [];
  } catch {
    // Branches surface is Pro+ — Starter tenants 200 with empty list,
    // Pro+ tenants get the dropdown. Either way, ignore errors here.
    return [];
  }
}

export default async function NewPromotionPage() {
  const branches = await loadBranches();
  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/promotions"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All promotions
        </Link>
      </div>

      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">New promotion</h1>
        <p className="text-sm text-muted-foreground">
          The bot mentions active promotions when a customer asks about pricing,
          deals, or discounts.
        </p>
      </header>

      <PromotionForm mode="create" branches={branches} />
    </div>
  );
}
