/**
 * Promotion edit page (Phase 8 PR-9).
 *
 * Server Component fetches the promotion + branch list in parallel
 * and hands off to the same form used for create. The form switches
 * on `mode="edit"` to drive PATCH instead of POST + show a delete
 * button.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { PromotionForm } from "@/components/promotions/PromotionForm";

interface PromotionDTO {
  id: string;
  tenant_id: string;
  tenant_branch_id: string | null;
  title: string;
  description: string;
  discount_text: string;
  terms: string | null;
  valid_from: string;
  valid_until: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

interface BranchDTO {
  id: string;
  name: string;
}

async function load(
  id: string,
): Promise<{ promo: PromotionDTO | null; branches: BranchDTO[] }> {
  const [promoRes, branchesRes] = await Promise.allSettled([
    apiFetchServer<PromotionDTO>(TenantRoutes.promotion(id)),
    apiFetchServer<BranchDTO[]>(TenantRoutes.branches()),
  ]);
  let promo: PromotionDTO | null = null;
  if (promoRes.status === "fulfilled") {
    promo = promoRes.value ?? null;
  } else if (
    promoRes.reason instanceof PolarisApiError &&
    promoRes.reason.status === 404
  ) {
    promo = null;
  } else {
    throw promoRes.reason;
  }
  const branches =
    branchesRes.status === "fulfilled" ? (branchesRes.value ?? []) : [];
  return { promo, branches };
}

export default async function PromotionEditPage({
  params,
}: {
  params: { promo_id: string };
}) {
  const { promo, branches } = await load(params.promo_id);
  if (!promo) {
    notFound();
  }

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
        <h1 className="text-3xl font-semibold tracking-tight">
          Edit promotion
        </h1>
      </header>

      <PromotionForm mode="edit" branches={branches} initial={promo} />
    </div>
  );
}
