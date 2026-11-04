/**
 * Audience screen layout — shared header + sub-nav (Phase 8 PR-8).
 *
 * Three tabs map to routes:
 *   - Members   → /audience
 *   - Opted-out → /audience/opted-out (GA-blocking PDPA viewer)
 *   - Import    → /audience/import
 *
 * Per portal_design_prompt.md §6, the Opted-out tab gets a persistent
 * disclaimer banner OUTSIDE this layout (it lives on the page itself)
 * so the warning is contextual.
 */

import { Users } from "lucide-react";

import { AudienceTabs } from "@/components/audience/AudienceTabs";

export default function AudienceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <Users className="h-5 w-5 text-muted-foreground" aria-hidden />
          <h1 className="text-3xl font-semibold tracking-tight">Audience</h1>
        </div>
        <p className="text-sm text-muted-foreground">
          Opted-in contacts you can broadcast to. Opt-outs are tracked
          separately for PDPA compliance.
        </p>
      </header>

      <AudienceTabs />

      {children}
    </div>
  );
}
