/**
 * Page Object: /cost and /cost/[tenant_id]
 *
 * Source: apps/admin/app/(admin)/cost/page.tsx
 *         apps/admin/app/(admin)/cost/[tenant_id]/page.tsx
 *         apps/admin/components/cost/TenantCostTable.tsx
 *         apps/admin/components/cost/CohereOverrideSubpanel.tsx
 *         apps/admin/components/cost/CostCuttingRevisitBanner.tsx
 *         apps/admin/components/cost/IntentTierBreakdown.tsx
 */

import type { Locator, Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class CostPage extends BasePage {
  // ── /cost (all-tenant) ──────────────────────────────────────────────────

  /** h1 "Cost Dashboard" */
  readonly heading: Locator;

  /**
   * The cost table — rows carry data-band="green|amber|red" on the <tr>.
   * // TODO(selector): confirm data-band is on <tr> not <td> — see TenantCostTable.tsx line 176-207
   */
  readonly costTable: Locator;

  /** Amber-banded rows in the all-tenant table */
  readonly amberRows: Locator;

  /** Red-banded rows in the all-tenant table */
  readonly redRows: Locator;

  /** "Cost % MRR" sort button (SortBtn col="cost_pct_of_mrr") */
  readonly sortByCostPctBtn: Locator;

  /** "Total Cost" sort button */
  readonly sortByTotalCostBtn: Locator;

  /**
   * Month picker — the page reads ?month=YYYYMM from searchParams.
   * There is no dedicated <input type="month"> in the source; navigation
   * is driven by URL params.
   * // TODO(selector): confirm if a month-picker control exists in the final UI
   */

  /** h2 "Cohere Rerank Overrides" subpanel header */
  readonly cohereSubpanelHeading: Locator;

  /** Rows in the Cohere override subpanel table */
  readonly cohereSubpanelRows: Locator;

  // ── /cost/[tenant_id] (per-tenant detail) ───────────────────────────────

  /** h1 "Tenant Cost Detail" */
  readonly detailHeading: Locator;

  /** Summary tile: "Total Cost" */
  readonly summaryTotalCostTile: Locator;

  /** Summary tile: "MRR" */
  readonly summaryMrrTile: Locator;

  /** Summary tile: "% of MRR" */
  readonly summaryPctTile: Locator;

  /** Summary tile: "Alert Band" */
  readonly summaryAlertBandTile: Locator;

  /** Breakdown tile: "Anthropic" */
  readonly breakdownAnthropicTile: Locator;

  /** Breakdown tile: "Cohere Rerank" */
  readonly breakdownCohereTile: Locator;

  /** Breakdown tile: "Supabase Storage" */
  readonly breakdownSupabaseTile: Locator;

  /** Breakdown tile: "OpenRouter" */
  readonly breakdownOpenRouterTile: Locator;

  /** h2 "Per-Intent-Tier Breakdown" */
  readonly intentTierHeading: Locator;

  /**
   * "4 weeks of data accumulated" cost-cutting revisit banner
   * (CostCuttingRevisitBanner — only shown when data >= 4 weeks old).
   */
  readonly costCuttingBanner: Locator;

  /** "← Back to cost dashboard" link on detail page */
  readonly backLink: Locator;

  /** "Tenant not found" empty card (404 state) */
  readonly notFoundCard: Locator;

  constructor(page: Page) {
    super(page);

    // all-tenant
    this.heading = page.getByRole("heading", { name: /cost dashboard/i });
    this.costTable = page.getByRole("table").first();
    this.amberRows = page
      .getByRole("row")
      .filter({ has: page.locator('[data-band="amber"]') });
    this.redRows = page
      .getByRole("row")
      .filter({ has: page.locator('[data-band="red"]') });
    this.sortByCostPctBtn = page.getByRole("button", {
      name: /cost.*%.*mrr|cost.*pct/i,
    });
    this.sortByTotalCostBtn = page.getByRole("button", {
      name: /total cost/i,
    });
    this.cohereSubpanelHeading = page.getByRole("heading", {
      name: /cohere rerank overrides/i,
    });
    this.cohereSubpanelRows = page.getByRole("table").nth(1).getByRole("row");

    // per-tenant detail
    this.detailHeading = page.getByRole("heading", {
      name: /tenant cost detail/i,
    });
    this.summaryTotalCostTile = page
      .getByText(/^total cost$/i)
      .locator("..")
      .locator("..");
    this.summaryMrrTile = page.getByText(/^mrr$/i).locator("..").locator("..");
    this.summaryPctTile = page
      .getByText(/^% of mrr$/i)
      .locator("..")
      .locator("..");
    this.summaryAlertBandTile = page
      .getByText(/^alert band$/i)
      .locator("..")
      .locator("..");
    this.breakdownAnthropicTile = page
      .getByText(/^anthropic$/i)
      .locator("..")
      .locator("..");
    this.breakdownCohereTile = page
      .getByText(/^cohere rerank$/i)
      .locator("..")
      .locator("..");
    this.breakdownSupabaseTile = page
      .getByText(/^supabase storage$/i)
      .locator("..")
      .locator("..");
    this.breakdownOpenRouterTile = page
      .getByText(/^openrouter$/i)
      .locator("..")
      .locator("..");
    this.intentTierHeading = page.getByRole("heading", {
      name: /per-intent-tier breakdown/i,
    });
    this.costCuttingBanner = page.getByText(/4 weeks of data accumulated/i);
    this.backLink = page.getByRole("link", {
      name: /back to cost dashboard/i,
    });
    this.notFoundCard = page.getByText(/tenant not found/i);
  }

  async gotoAllTenants(month?: string): Promise<void> {
    const url = month ? `/cost?month=${month}` : "/cost";
    await this.goto(url);
  }

  async gotoTenantDetail(tenantId: string, month?: string): Promise<void> {
    const url = month
      ? `/cost/${tenantId}?month=${month}`
      : `/cost/${tenantId}`;
    await this.goto(url);
  }

  /** Navigate to tenant detail via the "View" link in the all-tenant table row. */
  async clickViewForTenant(tenantId: string): Promise<void> {
    // Each row has a "View" link with href=/cost/{tenant_id}
    await this.page
      .getByRole("link", { name: /^view$/i })
      .filter({ has: this.page.locator(`[href="/cost/${tenantId}"]`) })
      .click();
  }

  /** Sort the table by clicking a column header button. */
  async sortBy(col: "cost_pct" | "total_cost"): Promise<void> {
    if (col === "cost_pct") {
      await this.sortByCostPctBtn.click();
    } else {
      await this.sortByTotalCostBtn.click();
    }
  }

  /** Column headers in the main table (for locating specific columns) */
  columnHeader(name: RegExp | string): Locator {
    return this.page.getByRole("columnheader", { name });
  }
}
