/**
 * QuotesPage POM — /quotes, /quotes/new
 *
 * Selectors confirmed against:
 *   apps/admin/app/(admin)/quotes/page.tsx
 *   apps/admin/app/(admin)/quotes/new/page.tsx
 *   apps/admin/components/quotes/QuoteGeneratorForm.tsx
 */

import type { Locator, Page } from "@playwright/test";

import { BasePage } from "../_support/pom";

export class QuotesPage extends BasePage {
  readonly heading: Locator;
  readonly generateQuoteButton: Locator;
  readonly emptyState: Locator;

  constructor(page: Page) {
    super(page);
    // h1 from quotes/page.tsx: "Sales Quotes"
    this.heading = page.getByRole("heading", { name: /sales quotes/i });
    // Link button text: "Generate quote"
    this.generateQuoteButton = page.getByRole("link", {
      name: /generate quote/i,
    });
    // Empty state card text from quotes/page.tsx
    this.emptyState = page.getByText(/no quotes yet/i);
  }

  async goto(): Promise<void> {
    await this.page.goto("/quotes");
    await this.page.waitForLoadState("networkidle");
  }

  /** Locate a quote row by quote_code (e.g. "POL-Q-…") or company name. */
  rowByText(text: string): Locator {
    return this.page.getByRole("row").filter({ hasText: text });
  }

  /** PDF link for a row — <a> with text "PDF" */
  pdfLinkInRow(text: string): Locator {
    return this.rowByText(text).getByRole("link", { name: /^pdf$/i });
  }

  /** Converted badge for a row */
  convertedBadgeInRow(text: string): Locator {
    return this.rowByText(text).getByText(/converted/i);
  }
}

export class QuoteGeneratorPage extends BasePage {
  // Selectors from QuoteGeneratorForm.tsx — ids: prospect-email, prospect-company,
  // target-tier, billing-cycle, signup-date
  readonly prospectEmailInput: Locator;
  readonly prospectCompanyInput: Locator;
  readonly targetTierSelect: Locator;
  readonly billingCycleSelect: Locator;
  readonly signupDateInput: Locator;
  readonly submitButton: Locator;
  readonly errorMessage: Locator;
  /** Live proration preview card */
  readonly livePreview: Locator;
  /** Success card after generation — "Quote X generated successfully." */
  readonly successCard: Locator;

  constructor(page: Page) {
    super(page);
    this.prospectEmailInput = page.locator("#prospect-email");
    this.prospectCompanyInput = page.locator("#prospect-company");
    this.targetTierSelect = page.locator("#target-tier");
    this.billingCycleSelect = page.locator("#billing-cycle");
    this.signupDateInput = page.locator("#signup-date");
    // Button text: "Generate quote"
    this.submitButton = page.getByRole("button", {
      name: /^generate quote$/i,
    });
    this.errorMessage = page.getByText(/unexpected error|validation/i);
    // "Live preview" card from ProrationPreviewCard — the <p> with exact text
    this.livePreview = page.locator("p", { hasText: /^Live preview$/i });
    // Success card text: "Quote POL-Q-… generated successfully."
    this.successCard = page.getByText(/generated successfully/i);
  }

  /**
   * The ProrationPreviewCard container — use this to scope assertions to
   * avoid strict-mode violations (e.g. the select option text also has "1,099"
   * and the page subtitle has "ratio").
   *
   * ProrationPreviewCard renders:
   *   <Card class="space-y-3 p-4 bg-muted/30">
   *     <p class="text-xs font-semibold … text-muted-foreground">Live preview</p>
   *     <div class="space-y-1">…</div>
   *   </Card>
   *
   * We identify it as the innermost div that directly contains the "Live preview"
   * paragraph and nothing outside the card.  Using `.filter({ hasText })` on a
   * narrow locator ensures we don't accidentally match ancestor containers.
   */
  livePreviewCard(): Locator {
    // The preview <p> is a direct child of the card's inner div.
    // Find the paragraph first, then navigate to its parent card container.
    return this.page
      .locator("p.text-xs.font-semibold", { hasText: /^Live preview$/i })
      .locator("xpath=./..");
  }

  async goto(): Promise<void> {
    await this.page.goto("/quotes/new");
    await this.page.waitForLoadState("networkidle");
  }

  /** PDF download link rendered after generation by PdfPreview component */
  pdfDownloadLink(): Locator {
    // TODO(selector): confirm exact text from PdfPreview.tsx — likely "Download PDF"
    return this.page.getByRole("link", { name: /download pdf/i });
  }
}
