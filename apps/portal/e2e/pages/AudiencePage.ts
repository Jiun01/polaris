/**
 * AudiencePage POM — /audience (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/audience/page.tsx
 *   - apps/portal/app/(portal)/audience/import/page.tsx
 *   - apps/portal/app/(portal)/audience/opted-out/page.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class AudiencePage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/audience");
  }

  async gotoImport() {
    await super.goto("/audience/import");
  }

  async gotoOptedOut() {
    await super.goto("/audience/opted-out");
  }

  // ── Members list ─────────────────────────────────────────────────────────────

  /** role="search" form wrapping the filter controls */
  get filterForm() {
    return this.page.getByRole("search");
  }

  get channelSelect() {
    return this.page.getByLabel(/filter by channel/i);
  }

  get intentSelect() {
    return this.page.getByLabel(/filter by intent/i);
  }

  get searchInput() {
    return this.page.getByRole("searchbox");
  }

  get filterButton() {
    return this.page.getByRole("button", { name: /^filter$/i });
  }

  get membersTable() {
    return this.page.getByRole("table");
  }

  get olderLink() {
    return this.page.getByRole("link", { name: /older/i });
  }

  get emptyHeading() {
    return this.page.getByRole("heading", { name: /no members yet/i });
  }

  get importCsvLink() {
    return this.page.getByRole("link", { name: /import a csv/i });
  }

  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  // ── Import page ──────────────────────────────────────────────────────────────

  /** The heading for CSV columns section on /audience/import */
  get csvColumnsHeading() {
    return this.page.getByRole("heading", { name: /csv columns/i });
  }

  // ── Opted-out page ───────────────────────────────────────────────────────────

  /** Persistent PDPA warning banner — role="alert" scoped to PDPA text to avoid
   *  strict-mode collision with the Next.js route announcer (also role="alert"). */
  get pdpaBanner() {
    return this.page.getByRole("alert").filter({ hasText: /pdpa notice/i });
  }

  get exportCsvButton() {
    return this.page.getByRole("link", { name: /export csv/i });
  }

  get optedOutTable() {
    return this.page.getByRole("table");
  }

  get noOptOutsHeading() {
    return this.page.getByRole("heading", { name: /no opt-outs yet/i });
  }
}
