/**
 * Shared TypeScript types for the Financial Suite (Phase 9 PR-7/8/9/10).
 *
 * TenantInvoice / PlanChangeRequest / SalesQuote below are HAND-MAINTAINED
 * BY DESIGN, not a TODO: audit-batch-f-frontend-consolidation (2026-07)
 * confirmed each admin router (admin_billing.py, admin_plan_changes.py,
 * admin_quotes.py) defines its OWN local `_TenantInvoice` / `_PlanChangeRow`
 * / `_SalesQuote` Pydantic response model — none of them use
 * `packages/shared-types`'s same-named models as `response_model`. Those
 * shared-types models (`polaris_shared_types.tenant_invoices.TenantInvoice`
 * etc.) back a DIFFERENT, tenant-facing surface with a divergent field set
 * (e.g. TenantInvoice there has `period`/`amount_myr`/`pdf_bucket`; this
 * admin view's real wire shape has `invoice_kind`/`subtotal_myr`/
 * `tax_myr`/`total_myr`/`period_start`/`period_end` instead) — importing
 * the shared-types version here would silently mistype every admin billing
 * page. Re-diff before ever swapping these three to a shared-types import.
 *
 * The cost-dashboard cluster below (TenantCostMonthly, CohereOverrideCost,
 * PlatformCostSummary, PlatformFixedCost, AlertBand, PerIntentTierBreakdown)
 * IS a genuine SSOT match — admin_cost.py imports those exact models from
 * `polaris_shared_types.tenant_cost` as its response_model — so those are
 * re-exported from @polaris/shared-types instead of hand-declared.
 */

export interface TenantInvoice {
  id: string;
  tenant_id: string;
  tenant_name?: string | null;
  invoice_number: string;
  invoice_kind:
    | "monthly_subscription"
    | "annual_subscription"
    | "plan_change_proration"
    | "addon_seat";
  period_start: string;
  period_end: string;
  subtotal_myr: number;
  tax_myr: number;
  total_myr: number;
  status: "issued" | "paid" | "overdue" | "void";
  issued_at: string;
  due_at: string;
  paid_at: string | null;
}

export interface PlanChangeRequest {
  id: string;
  tenant_id: string;
  tenant_name?: string;
  current_tier: string;
  target_tier: string;
  current_billing_cycle: string;
  target_billing_cycle: string;
  status: "pending" | "approved" | "rejected";
  requested_at: string;
  proration_snapshot?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Cost dashboard types (Phase 9 PR-10) — re-exported from @polaris/shared-types.
// admin_cost.py's response_model IS polaris_shared_types.tenant_cost's
// TenantCostMonthly/CohereOverrideCost/PlatformCostSummary/PlatformFixedCost
// directly (confirmed during audit-batch-f-frontend-consolidation, 2026-07)
// — a true SSOT match, unlike TenantInvoice/PlanChangeRequest/SalesQuote above.
// ---------------------------------------------------------------------------

export type {
  AlertBand,
  CohereOverrideCost,
  PerIntentTierBreakdown,
  PlatformCostSummary,
  PlatformFixedCost,
  TenantCostMonthly,
} from "@polaris/shared-types";

export interface AllTenantsCostResponse {
  items: import("@polaris/shared-types").TenantCostMonthly[];
  period_yyyymm: string;
  data_source: string;
}

export interface CohereOverridesResponse {
  items: import("@polaris/shared-types").CohereOverrideCost[];
  period_yyyymm: string;
}

export interface SalesQuote {
  id: string;
  quote_code: string;
  prospect_email: string | null;
  prospect_company: string | null;
  target_tier: string;
  target_billing_cycle: string;
  intended_signup_date: string;
  expires_at: string;
  converted_to_tenant_id: string | null;
  converted_at: string | null;
  created_at: string;
}
