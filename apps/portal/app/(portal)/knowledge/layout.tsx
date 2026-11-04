/**
 * Knowledge screen layout (Phase 8 PR-6).
 *
 * Wraps the three tab routes — /knowledge (Sources), /knowledge/runs,
 * and the chunks drill-down — with a shared tabs nav + page header.
 * Sources is the default landing tab per portal_design_prompt.md §3.
 *
 * The drill-down at `/knowledge/chunks/[doc_id]` lives inside this
 * layout too so its breadcrumb-style header is rendered consistently,
 * but the active tab indicator falls back to Sources because that's
 * the parent surface a user would expect to return to.
 */

import { Brain } from "lucide-react";

import { KnowledgeTabs } from "@/components/knowledge/KnowledgeTabs";

export default function KnowledgeLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <Brain className="h-5 w-5 text-muted-foreground" aria-hidden />
          <h1 className="text-3xl font-semibold tracking-tight">Knowledge</h1>
        </div>
        <p className="text-sm text-muted-foreground">
          Documents and text your bot can pull from when answering customers.
          New sources typically take up to 6 hours to ingest.
        </p>
      </header>

      <KnowledgeTabs />

      {children}
    </div>
  );
}
