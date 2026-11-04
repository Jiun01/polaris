/**
 * TemplatesPage POM — /templates and /templates/new (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/templates/page.tsx
 *   - apps/portal/app/(portal)/templates/new/page.tsx
 *   - apps/portal/components/templates/TemplateForm.tsx
 *
 * Key ARIA anchors:
 *   - "New template" CTA:  role="link" name=/new template/i
 *   - Form steps nav:      role="list" aria-label="Steps"
 *   - Template name input: getByLabel(/template name/i)
 *   - Language input:      getByLabel(/language code/i)
 *   - Category fieldset:   role="group" / legend "Category"
 *   - Body textarea:       getByLabel(/body/i)
 *   - "Next: Compose" btn: role="button" name=/next.*compose/i
 *   - "Next: Preview" btn: role="button" name=/next.*preview/i
 *   - "Submit to Meta" btn:role="button" name=/submit to meta/i
 *   - Error alert:         role="alert"
 *   - No-WA CTA:           "Connect WhatsApp before submitting templates"
 *   - WhatsApp inbox select: getByLabel(/whatsapp inbox/i) — only when >1 channel
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class TemplatesPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/templates");
  }

  async gotoNew() {
    await super.goto("/templates/new");
  }

  // ---- List page ----

  get heading() {
    return this.page.getByRole("heading", { name: /^templates$/i });
  }

  get newTemplateButton() {
    return this.page.getByRole("link", { name: /new template/i });
  }

  get emptyState() {
    return this.page.getByText(/no templates yet/i);
  }

  /** Fetch-error banner — role="status". */
  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  /**
   * A status badge cell for a given template name row.
   * The badge text is one of: pending / approved / rejected / paused / disabled.
   */
  statusBadge(
    status: "pending" | "approved" | "rejected" | "paused" | "disabled",
  ) {
    return this.page.getByText(new RegExp(`^${status}$`, "i")).first();
  }

  /** Rejection reason text (visible only on rejected rows). */
  rejectionReason(text: string | RegExp) {
    return this.page.getByText(text);
  }

  // ---- New template form (/templates/new) ----

  get newHeading() {
    return this.page.getByRole("heading", { name: /new template/i });
  }

  /** No-WhatsApp CTA heading. */
  get noWhatsAppCta() {
    return this.page.getByText(/connect whatsapp before submitting templates/i);
  }

  /** Step indicator list — aria-label="Steps". */
  get stepsNav() {
    return this.page.getByLabel(/^steps$/i);
  }

  // Step 1 fields

  /** Template name input — labelled "Template name (lower_snake)". */
  get nameInput() {
    return this.page.getByLabel(/template name/i);
  }

  /** Language code input. */
  get languageInput() {
    return this.page.getByLabel(/language code/i);
  }

  /**
   * Category radio card by value.
   * The radio is visually hidden (sr-only); we click the visible label text.
   */
  categoryCard(category: "Utility" | "Marketing" | "Authentication") {
    return this.page.getByText(category, { exact: true }).first();
  }

  /** "Next: Compose" button. */
  get nextComposeButton() {
    return this.page.getByRole("button", { name: /next.*compose/i });
  }

  /** WhatsApp inbox picker — shown only when the tenant has >1 WA channel. */
  get channelPicker() {
    return this.page.getByLabel(/whatsapp inbox/i);
  }

  // Step 2 fields

  /** Header text input. */
  get headerInput() {
    return this.page.getByLabel(/^header/i);
  }

  /** Body textarea. */
  get bodyTextarea() {
    return this.page.getByLabel(/^body$/i);
  }

  /** Footer text input. */
  get footerInput() {
    return this.page.getByLabel(/^footer/i);
  }

  /** "Next: Preview" button. */
  get nextPreviewButton() {
    return this.page.getByRole("button", { name: /next.*preview/i });
  }

  // Step 3

  /** Preview bubble container — contains the rendered body text. */
  get previewBubble() {
    // TODO(selector): confirm selector — the preview div has class rounded-2xl
    // bg-emerald-50 but no testid or ARIA role. Match by visible label text.
    return this.page.getByText(/^preview$/i).first();
  }

  /** "Submit to Meta" button. */
  get submitButton() {
    return this.page.getByRole("button", { name: /submit to meta/i });
  }

  /** Error alert on Step 3. */
  get errorAlert() {
    return this.page.getByRole("alert");
  }

  /** Back buttons (Step 2→1 and Step 3→2). */
  get backButton() {
    return this.page.getByRole("button", { name: /^back$/i });
  }
}
