/**
 * Knowledge Gaps content (Knowledge Gap Detection M3).
 *
 * Pure render of the ranked gap themes. Server-renderable (no client hooks) so
 * it can be passed as children to the client-side tier guard. DTO types are
 * imported from @polaris/shared-types — confirmed a true 1:1 SSOT match
 * during audit-batch-f-frontend-consolidation (2026-07):
 * tenant_knowledge_gaps.py's GET /api/tenant/knowledge-gaps/summary uses
 * KnowledgeGapsSummary directly as response_model.
 *
 * The actual list (sort/filter/drill-down) lives in KnowledgeGapsList which is
 * a "use client" island — this component stays server-renderable.
 */

import type {
  KnowledgeGapTheme,
  KnowledgeGapsSummary,
} from "@polaris/shared-types";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KnowledgeGapsList } from "./KnowledgeGapsList";

export type KnowledgeGapThemeDTO = KnowledgeGapTheme;
export type KnowledgeGapsSummaryDTO = KnowledgeGapsSummary;

export function KnowledgeGapsContent({
  summary,
}: {
  summary: KnowledgeGapsSummaryDTO;
}) {
  if (summary.themes.length === 0) {
    return (
      <Card className="border-dashed">
        <CardHeader>
          <CardTitle>No knowledge gaps yet</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Your bot is answering confidently. When a customer asks something it
          can&apos;t answer, the question shows up here so you can fill the gap.
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {summary.total_misses} unanswered{" "}
        {summary.total_misses === 1 ? "question" : "questions"} across{" "}
        {summary.themes.length}{" "}
        {summary.themes.length === 1 ? "theme" : "themes"} in the last{" "}
        {summary.window_days} days.
      </p>

      <KnowledgeGapsList
        themes={summary.themes}
        windowDays={summary.window_days}
      />
    </div>
  );
}
