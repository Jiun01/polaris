/**
 * KnowledgePage POM — /knowledge, /knowledge/runs, /knowledge/chunks/[doc_id]
 * (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/knowledge/page.tsx
 *   - apps/portal/components/knowledge/CapacityBar.tsx
 *   - apps/portal/components/knowledge/AddKnowledgeButton.tsx
 *   - apps/portal/components/knowledge/KnowledgeSourcesTable.tsx
 *   - apps/portal/app/(portal)/knowledge/runs/page.tsx
 *   - apps/portal/app/(portal)/knowledge/chunks/[doc_id]/page.tsx
 *
 * Key ARIA anchors:
 *   - CapacityBar:      role="progressbar" aria-label="Knowledge storage used"
 *   - AddKnowledge btn: Button text "Add knowledge" (DialogTrigger)
 *   - Dialog title:     "Add a knowledge source"
 *   - Tab buttons:      role="tab" (URL / Text / File)
 *   - URL input:        Label "Website URL"
 *   - Cancel btn:       aria-label="Cancel ingestion request" (per-row)
 *   - Error alert:      role="alert" (inside dialog)
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class KnowledgePage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/knowledge");
  }

  async gotoRuns() {
    await super.goto("/knowledge/runs");
  }

  async gotoChunks(docId: string) {
    await super.goto(`/knowledge/chunks/${docId}`);
  }

  /** CapacityBar progressbar. */
  get capacityBar() {
    return this.page.getByRole("progressbar", {
      name: /knowledge storage used/i,
    });
  }

  /**
   * "Add knowledge" trigger button.
   *
   * The knowledge page renders TWO AddKnowledgeButton instances
   * simultaneously (one in the header, one in the empty-state card).
   * Use .first() to avoid strict-mode violation when both are present.
   */
  get addKnowledgeButton() {
    return this.page.getByRole("button", { name: /add knowledge/i }).first();
  }

  // ---- AddKnowledgeButton dialog ----

  get dialogTitle() {
    return this.page.getByRole("heading", {
      name: /add a knowledge source/i,
    });
  }

  /** Tab bar inside the dialog — role="tablist". */
  get tabList() {
    return this.page.getByRole("tablist");
  }

  tabButton(label: "URL" | "Text" | "File") {
    return this.page.getByRole("tab", { name: label });
  }

  /** URL tab: Website URL input. */
  get urlInput() {
    return this.page.getByLabel(/website url/i);
  }

  /** Text tab: Paste text textarea. */
  get textInput() {
    return this.page.getByLabel(/paste text/i);
  }

  /** File tab: file input labelled "Document or image". */
  get fileInput() {
    return this.page.getByLabel(/document or image/i);
  }

  /** Error paragraph inside the dialog — role="alert". */
  get dialogError() {
    return this.page.getByRole("alert");
  }

  /** "Add URL" submit button. */
  get addUrlButton() {
    return this.page.getByRole("button", { name: /add url/i });
  }

  /** "Add text" submit button. */
  get addTextButton() {
    return this.page.getByRole("button", { name: /add text/i });
  }

  /** "Upload file" submit button. */
  get uploadFileButton() {
    return this.page.getByRole("button", { name: /upload file/i });
  }

  /** Cancel dialog button ("Cancel" in the footer). */
  get dialogCancelButton() {
    return this.page.getByRole("button", { name: /^cancel$/i });
  }

  // ---- Sources table ----

  /** Empty-state heading. */
  get emptyState() {
    return this.page.getByText(/no sources yet/i);
  }

  /** Fetch-error banner. */
  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  /**
   * "Cancel ingestion request" icon button — appears only on pending rows.
   * Returns all matching (use .first() for a single row).
   */
  get cancelPendingButton() {
    return this.page.getByRole("button", {
      name: /cancel ingestion request/i,
    });
  }

  // ---- Runs page ----

  get runsEmptyState() {
    return this.page.getByText(/no ingestion runs yet/i);
  }

  // ---- Chunks / doc detail ----

  get chunkDetailHeading() {
    return this.page.getByRole("heading", { name: /source details/i });
  }

  /** "Chunks added" definition term. */
  get chunksAddedLabel() {
    return this.page.getByText(/chunks added/i);
  }
}
