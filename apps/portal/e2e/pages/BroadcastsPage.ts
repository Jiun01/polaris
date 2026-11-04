/**
 * BroadcastsPage POM — /broadcasts (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/broadcasts/page.tsx
 *   - apps/portal/app/(portal)/broadcasts/locked/page.tsx
 *   - apps/portal/app/(portal)/broadcasts/new/page.tsx
 *   - apps/portal/components/broadcasts/BroadcastWizard.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class BroadcastsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/broadcasts");
  }

  async gotoNew() {
    await super.goto("/broadcasts/new");
  }

  async gotoLocked() {
    await super.goto("/broadcasts/locked");
  }

  // ── List page ──────────────────────────────────────────────────────────────

  get heading() {
    return this.page.getByRole("heading", { name: /^broadcasts$/i });
  }

  get newBroadcastButton() {
    return this.page.getByRole("link", { name: /new broadcast/i });
  }

  get broadcastsTable() {
    return this.page.getByRole("table");
  }

  get emptyHeading() {
    return this.page.getByRole("heading", { name: /no broadcasts yet/i });
  }

  get startBroadcastLink() {
    return this.page.getByRole("link", { name: /start a broadcast/i });
  }

  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  // ── Locked page ─────────────────────────────────────────────────────────────

  get lockedHeading() {
    // h2 inside the card: "Broadcasts are a Pro feature"
    return this.page.getByRole("heading", {
      name: /broadcasts are a pro feature/i,
    });
  }

  get upgradeToProLink() {
    return this.page.getByRole("link", { name: /upgrade to pro/i });
  }

  get comparePlansLink() {
    return this.page.getByRole("link", { name: /compare plans/i });
  }

  // ── New broadcast wizard ────────────────────────────────────────────────────

  get wizardStepsList() {
    return this.page.getByRole("list", { name: /steps/i });
  }

  get newBroadcastHeading() {
    return this.page.getByRole("heading", { name: /^new broadcast$/i });
  }

  /** Step 1 — broadcast name input (id="bcast-name") */
  get nameInput() {
    return this.page.locator("#bcast-name");
  }

  /** Step 1 — template select (id="bcast-template") */
  get templateSelect() {
    return this.page.locator("#bcast-template");
  }

  /** Step 1 — WhatsApp inbox select (id="bcast-channel-inbox"), only
   *  rendered when the tenant has more than one WhatsApp channel. */
  get channelInboxSelect() {
    return this.page.locator("#bcast-channel-inbox");
  }

  get nextAudienceButton() {
    return this.page.getByRole("button", { name: /next: audience/i });
  }

  /** Step 2 — channel select (id="bcast-channel") */
  get channelSelect() {
    return this.page.locator("#bcast-channel");
  }

  get tagsInput() {
    return this.page.locator("#bcast-tags");
  }

  get nextScheduleButton() {
    return this.page.getByRole("button", { name: /next: schedule/i });
  }

  /** Step 3 — "Send immediately" radio */
  get sendNowRadio() {
    return this.page.getByRole("radio", { name: /send immediately/i });
  }

  get scheduleLaterRadio() {
    return this.page.getByRole("radio", { name: /schedule for later/i });
  }

  get sendBroadcastButton() {
    return this.page.getByRole("button", { name: /send broadcast/i });
  }

  get scheduleBroadcastButton() {
    return this.page.getByRole("button", { name: /schedule broadcast/i });
  }

  get wizardErrorAlert() {
    return this.page.getByRole("alert");
  }

  /** No-approved-template CTA */
  get noApprovedTemplateHeading() {
    return this.page.getByRole("heading", {
      name: /no approved templates yet/i,
    });
  }

  get submitTemplateLink() {
    return this.page.getByRole("link", { name: /submit a template/i });
  }
}
