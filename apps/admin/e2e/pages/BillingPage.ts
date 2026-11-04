/**
 * BillingPage POM — /billing, /billing/[invoice_id], /billing/new/*
 *
 * Selectors confirmed against:
 *   apps/admin/app/(admin)/billing/page.tsx
 *   apps/admin/app/(admin)/billing/[invoice_id]/page.tsx
 *   apps/admin/app/(admin)/billing/new/select/page.tsx
 *   apps/admin/app/(admin)/billing/new/[tenant_id]/page.tsx
 *   apps/admin/components/billing/MarkPaidButton.tsx
 *   apps/admin/components/billing/IssueInvoiceForm.tsx
 */

import type { Locator, Page } from "@playwright/test";

import { BasePage } from "../_support/pom";

export class BillingPage extends BasePage {
  readonly heading: Locator;
  readonly issueInvoiceButton: Locator;

  constructor(page: Page) {
    super(page);
    // h1 from billing/page.tsx: "Billing"
    this.heading = page.getByRole("heading", { name: /^billing$/i });
    // "Issue invoice" button/link in billing/page.tsx
    this.issueInvoiceButton = page.getByRole("link", {
      name: /issue invoice/i,
    });
  }

  /** Open the billing list, optionally on a specific tab. */
  async open(tab?: "overdue" | "this_month"): Promise<void> {
    const url = tab ? `/billing?tab=${tab}` : "/billing";
    await this.page.goto(url);
    await this.page.waitForLoadState("networkidle");
  }

  /** Tab links — text from TABS array: "All", "Overdue", "This Month" */
  tabLink(name: "All" | "Overdue" | "This Month"): Locator {
    return this.page.getByRole("link", { name: new RegExp(`^${name}$`, "i") });
  }

  /** Locate a row in the invoice table by invoice number or tenant name fragment. */
  rowByText(text: string): Locator {
    return this.page.getByRole("row").filter({ hasText: text });
  }
}

export class InvoiceDetailPage extends BasePage {
  // "Mark Paid" button text from MarkPaidButton.tsx
  readonly markPaidButton: Locator;
  // "Download PDF" button text from [invoice_id]/page.tsx
  readonly downloadPdfButton: Locator;

  constructor(page: Page) {
    super(page);
    this.markPaidButton = page.getByRole("button", { name: /mark paid/i });
    this.downloadPdfButton = page.getByRole("button", {
      name: /download pdf/i,
    });
  }

  async goto(invoiceId: string): Promise<void> {
    await this.page.goto(`/billing/${invoiceId}`);
    await this.page.waitForLoadState("networkidle");
  }

  /** Status badge — text content (Issued | Paid | Overdue | Void) */
  statusBadge(): Locator {
    // The badge is a <span>; scope to span so we don't also match the "Void"
    // action <button> rendered for issued invoices (strict-mode collision).
    return this.page
      .getByText(/^(Issued|Paid|Overdue|Void)$/)
      .and(this.page.locator("span"));
  }
}

export class IssueInvoicePage extends BasePage {
  // Selectors from IssueInvoiceForm.tsx — ids are invoice-kind, period-start, etc.
  readonly kindSelect: Locator;
  readonly periodStartInput: Locator;
  readonly periodEndInput: Locator;
  readonly subtotalInput: Locator;
  readonly taxInput: Locator;
  readonly dueAtInput: Locator;
  readonly submitButton: Locator;
  readonly prorationNotice: Locator;

  constructor(page: Page) {
    super(page);
    this.kindSelect = page.locator("#invoice-kind");
    this.periodStartInput = page.locator("#period-start");
    this.periodEndInput = page.locator("#period-end");
    this.subtotalInput = page.locator("#subtotal");
    this.taxInput = page.locator("#tax");
    this.dueAtInput = page.locator("#due-at");
    // Button text: "Issue invoice"
    this.submitButton = page.getByRole("button", { name: /^issue invoice$/i });
    // Proration notice paragraph — text from IssueInvoiceForm.tsx
    this.prorationNotice = page.getByText(/proration invoices are normally/i);
  }

  async gotoSelectTenant(): Promise<void> {
    await this.page.goto("/billing/new/select");
    await this.page.waitForLoadState("networkidle");
  }

  async gotoForTenant(tenantId: string): Promise<void> {
    await this.page.goto(`/billing/new/${tenantId}`);
    await this.page.waitForLoadState("networkidle");
  }

  /**
   * Live total display — the containing div that wraps both the "Total: "
   * label span and the formatted amount span from IssueInvoiceForm.tsx.
   * Use this for toContainText assertions (the label span alone has no amount).
   */
  liveTotalContainer(): Locator {
    // The div has class bg-muted/50 and contains "Total:" as child text.
    return this.page.locator("div.rounded-md").filter({ hasText: /^Total:/i });
  }
}
