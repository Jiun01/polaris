/**
 * TenantDetailPage POM — /tenants/[id]
 *
 * Selectors confirmed against:
 *   apps/admin/app/(admin)/tenants/[tenant_id]/page.tsx
 *   apps/admin/components/tenants/AddChannelModal.tsx
 *   apps/admin/components/tenants/SuspendButton.tsx
 *   apps/admin/components/tenants/DeleteConfirmation.tsx
 */

import type { Locator, Page } from "@playwright/test";

import { BasePage } from "../_support/pom";

export class TenantDetailPage extends BasePage {
  // Tab links — from TABS const in page.tsx: overview/channels/templates/branches/lifecycle
  // Rendered as <Link aria-current="page"> with className text-sm font-medium capitalize
  readonly overviewTab: Locator;
  readonly channelsTab: Locator;
  readonly templatesTab: Locator;
  readonly branchesTab: Locator;
  readonly lifecycleTab: Locator;

  // Lifecycle tab
  readonly suspendButton: Locator;
  readonly unsuspendButton: Locator;
  readonly beginDeletionButton: Locator;

  // Add Channel Modal trigger — "Add channel" button in ChannelsTabActions
  readonly addChannelButton: Locator;

  constructor(page: Page) {
    super(page);
    // Tab nav links — scope to <main> to avoid collisions with sidebar nav links
    // that share the same text (e.g. sidebar "Channels" vs tab "channels").
    const main = page.getByRole("main");
    this.overviewTab = main.getByRole("link", { name: /^overview$/i });
    this.channelsTab = main.getByRole("link", { name: /^channels$/i });
    this.templatesTab = main.getByRole("link", { name: /^templates$/i });
    this.branchesTab = main.getByRole("link", { name: /^branches$/i });
    this.lifecycleTab = main.getByRole("link", { name: /^lifecycle$/i });

    // SuspendButton text: "Suspend" / "Unsuspend"
    this.suspendButton = page.getByRole("button", { name: /^suspend$/i });
    this.unsuspendButton = page.getByRole("button", { name: /^unsuspend$/i });

    // "Begin deletion" link in LifecycleTab
    this.beginDeletionButton = page.getByRole("link", {
      name: /begin deletion/i,
    });

    // AddChannelModal trigger — Button text: "Add channel"
    this.addChannelButton = page.getByRole("button", { name: /add channel/i });
  }

  async goto(tenantId: string, tab?: string): Promise<void> {
    const url = tab
      ? `/tenants/${tenantId}?tab=${tab}`
      : `/tenants/${tenantId}`;
    await this.page.goto(url);
    await this.page.waitForLoadState("networkidle");
  }

  // ------------------------------------------------------------------
  // Add Channel Modal helpers
  // Selectors from AddChannelModal.tsx: id="ac-kind", id="ac-cw-id", etc.
  // ------------------------------------------------------------------

  /** The channel kind <select id="ac-kind"> */
  channelKindSelect(): Locator {
    return this.page.locator("#ac-kind");
  }

  /** Chatwoot inbox ID <Input id="ac-cw-id"> */
  chatwootInboxIdInput(): Locator {
    return this.page.locator("#ac-cw-id");
  }

  /** WABA ID input (WhatsApp only) — id="ac-waba" */
  wabaIdInput(): Locator {
    return this.page.locator("#ac-waba");
  }

  /** Phone number ID input — id="ac-phone" */
  phoneNumberIdInput(): Locator {
    return this.page.locator("#ac-phone");
  }

  /** Instagram Business Account ID — id="ac-ig" */
  igBusinessIdInput(): Locator {
    return this.page.locator("#ac-ig");
  }

  /** Facebook Page ID — id="ac-fb" */
  fbPageIdInput(): Locator {
    return this.page.locator("#ac-fb");
  }

  /** Override tier cap checkbox — id="ac-override" */
  overrideTierCapCheckbox(): Locator {
    return this.page.locator("#ac-override");
  }

  /** Override reason textarea — id="ac-reason"; only visible when override is checked */
  overrideReasonTextarea(): Locator {
    return this.page.locator("#ac-reason");
  }

  /** "Add channel" submit button inside the dialog footer */
  addChannelSubmitButton(): Locator {
    return this.page
      .getByRole("dialog")
      .getByRole("button", { name: /^add channel$/i });
  }

  /** Error alert inside the modal */
  addChannelErrorAlert(): Locator {
    return this.page.getByRole("dialog").getByRole("alert");
  }

  // ------------------------------------------------------------------
  // Delete page helpers (separate route /tenants/[id]/delete)
  // Selectors from DeleteConfirmation.tsx
  // ------------------------------------------------------------------

  /** Input id="confirm" — type slug to confirm deletion */
  deleteConfirmInput(): Locator {
    return this.page.locator("#confirm");
  }

  /** "Hard-delete tenant" destructive button */
  hardDeleteButton(): Locator {
    return this.page.getByRole("button", { name: /hard-delete tenant/i });
  }
}
