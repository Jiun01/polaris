/**
 * ChannelsPage POM — /channels (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/channels/page.tsx
 *   - apps/portal/components/channels/ChannelsClient.tsx
 *   - apps/portal/components/channels/ChannelTile.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class ChannelsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/channels");
  }

  get heading() {
    return this.page.getByRole("heading", { name: /^channels$/i });
  }

  /** Fetch-error banner — role="status" */
  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  // ── Channel tiles ─────────────────────────────────────────────────────────────
  // ChannelTile renders heading text for the channel name. The page is a
  // read-only status view: no connect button or support modal.

  get whatsAppTile() {
    return this.page.getByText(/whatsapp/i).first();
  }

  get instagramTile() {
    return this.page.getByText(/instagram/i).first();
  }

  get messengerTile() {
    return this.page.getByText(/facebook messenger/i).first();
  }
}
