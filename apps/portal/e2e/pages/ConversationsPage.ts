/**
 * ConversationsPage POM — /conversations and /conversations/[id] (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/conversations/page.tsx
 *   - apps/portal/components/conversations/ChannelFilterChips.tsx
 *   - apps/portal/components/conversations/ConversationsTable.tsx
 *   - apps/portal/app/(portal)/conversations/[conversation_id]/page.tsx
 *   - apps/portal/components/conversations/ConversationTranscript.tsx
 *
 * Key ARIA anchors:
 *   - Channel filter nav: role="navigation" name="Filter conversations by channel"
 *   - "Older →" pagination link: getByRole("link", { name: /older/i })
 *   - Transcript list: role="list" aria-label="Conversation transcript"  (ol)
 *   - "Reply in inbox" button: aria-label contains "Reply to this conversation"
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class ConversationsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/conversations");
  }

  async gotoDetail(conversationId: string) {
    await super.goto(`/conversations/${conversationId}`);
  }

  get heading() {
    return this.page.getByRole("heading", { name: /^conversations$/i });
  }

  /** Channel filter nav wrapping the chip links. */
  get filterNav() {
    return this.page.getByRole("navigation", {
      name: /filter conversations by channel/i,
    });
  }

  /** Individual channel filter chip by label text. */
  channelChip(label: "All" | "WhatsApp" | "Instagram" | "Messenger" | "Demo") {
    return this.filterNav.getByText(label);
  }

  /** "Older →" pagination link rendered by ConversationsTable when next_cursor exists. */
  get olderLink() {
    return this.page.getByRole("link", { name: /older/i });
  }

  /** Empty-state container — visible when items.length === 0 and no error. */
  get emptyState() {
    return this.page.getByText(/no conversations yet/i);
  }

  /** Fetch-error banner — role="status" with warning text. */
  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  // ---- Detail page ----

  get detailHeading() {
    // h1 = customer_display_name — dynamically set; assert by role only.
    return this.page.getByRole("heading", { level: 1 });
  }

  /** "Reply in inbox" CTA — aria-label set in [conversation_id]/page.tsx. */
  get replyInInboxButton() {
    return this.page.getByRole("link", {
      name: /reply to this conversation in your messaging inbox/i,
    });
  }

  /** The transcript ordered list — aria-label="Conversation transcript". */
  get transcriptList() {
    return this.page.getByRole("list", { name: /conversation transcript/i });
  }

  /** Back link to /conversations. */
  get backLink() {
    return this.page.getByRole("link", { name: /all conversations/i });
  }
}
