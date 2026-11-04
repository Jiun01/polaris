"use client";

/**
 * Knowledge Gaps list island (Knowledge Gap Detection M3 — Phase A + B).
 *
 * Client component that owns sort/filter state over the ≤50-item themes
 * array received as a prop (no additional network calls for sort/filter).
 * Task 4: sort control + signal-type filter.
 * Task 7: expandable cards that lazily fetch contributing conversations.
 */

import * as React from "react";
import Link from "next/link";

import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type {
  KnowledgeGapExample,
  KnowledgeGapExamplesResponse,
} from "@polaris/shared-types";

import { apiFetch } from "@/lib/api/client";
import { TenantRoutes } from "@/lib/api/routes";
import type { KnowledgeGapThemeDTO } from "./KnowledgeGapsContent";

// ---------------------------------------------------------------------------
// Sort / filter types
// ---------------------------------------------------------------------------

type SortKey = "asks" | "recent" | "last_seen";
type SignalFilter = "all" | "no_retrieval" | "low_retrieval" | "handoff";

// ---------------------------------------------------------------------------
// Example (drill-down) types — from @polaris/shared-types. Confirmed a true
// 1:1 SSOT match (audit-batch-f-frontend-consolidation, 2026-07):
// tenant_knowledge_gaps.py's GET .../summary/examples uses
// KnowledgeGapExamplesResponse directly as response_model.
// ---------------------------------------------------------------------------

type KnowledgeGapExampleDTO = KnowledgeGapExample;
type KnowledgeGapExamplesResponseDTO = KnowledgeGapExamplesResponse;

// ---------------------------------------------------------------------------
// Helpers (moved from KnowledgeGapsContent)
// ---------------------------------------------------------------------------

/** Trend hint: at least half of all asks landed in the last 7 days. */
function isRising(theme: KnowledgeGapThemeDTO): boolean {
  return theme.ask_count > 0 && theme.recent_count * 2 >= theme.ask_count;
}

function signalLine(theme: KnowledgeGapThemeDTO): string {
  const parts = [
    theme.no_retrieval_count > 0
      ? `${theme.no_retrieval_count} no answer found`
      : null,
    theme.low_retrieval_count > 0
      ? `${theme.low_retrieval_count} weak match`
      : null,
    theme.handoff_count > 0 ? `${theme.handoff_count} sent to a human` : null,
  ].filter(Boolean);
  return `${parts.join(" · ")} · last asked ${theme.last_seen.slice(0, 10)}`;
}

// ---------------------------------------------------------------------------
// Per-theme expandable drill-down
// ---------------------------------------------------------------------------

function ThemeExamples({
  normQ,
  windowDays,
}: {
  normQ: string;
  windowDays: number;
}) {
  const [state, setState] = React.useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "done"; examples: KnowledgeGapExampleDTO[] }
    | { status: "error"; message: string }
  >({ status: "idle" });

  // Fetch on first mount (parent only mounts when expanded)
  React.useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    apiFetch<KnowledgeGapExamplesResponseDTO>(
      TenantRoutes.knowledgeGapsExamples(normQ, windowDays),
    )
      .then((res) => {
        if (cancelled) return;
        setState({ status: "done", examples: res?.examples ?? [] });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg =
          err instanceof Error ? err.message : "Failed to load conversations.";
        setState({ status: "error", message: msg });
      });
    return () => {
      cancelled = true;
    };
  }, [normQ, windowDays]);

  if (state.status === "loading") {
    return (
      <p className="mt-2 text-xs text-muted-foreground">
        Loading conversations…
      </p>
    );
  }

  if (state.status === "error") {
    return <p className="mt-2 text-xs text-destructive">{state.message}</p>;
  }

  if (state.status === "done") {
    if (state.examples.length === 0) {
      return (
        <p className="mt-2 text-xs text-muted-foreground">
          No contributing conversations found.
        </p>
      );
    }
    return (
      <ul className="mt-3 space-y-1.5 border-t pt-3">
        {state.examples.map((ex) => (
          <li key={ex.conversation_id}>
            <Link
              href={`/conversations/${ex.conversation_id}`}
              className="group flex flex-col gap-0.5 rounded-sm px-1 py-1 hover:bg-muted/50"
            >
              <span className="text-xs font-medium group-hover:underline">
                &ldquo;{ex.customer_question}&rdquo;
              </span>
              <span className="text-[11px] text-muted-foreground">
                {ex.created_at.slice(0, 10)} · {ex.signal_type}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    );
  }

  // idle — should not render (parent mounts only on expand trigger)
  return null;
}

// ---------------------------------------------------------------------------
// Main list component
// ---------------------------------------------------------------------------

export function KnowledgeGapsList({
  themes,
  windowDays,
}: {
  themes: KnowledgeGapThemeDTO[];
  windowDays: number;
}) {
  const [sortKey, setSortKey] = React.useState<SortKey>("asks");
  const [signalFilter, setSignalFilter] = React.useState<SignalFilter>("all");
  // Track which theme cards are expanded by their stable norm_q key (NOT array
  // index) so the right card stays open even if `themes` is reordered/refreshed.
  const [expandedNormQs, setExpandedNormQs] = React.useState<Set<string>>(
    new Set(),
  );

  const displayedThemes = React.useMemo(() => {
    // 1. Filter
    let filtered = themes;
    if (signalFilter !== "all") {
      filtered = themes.filter((t) => {
        if (signalFilter === "no_retrieval") return t.no_retrieval_count > 0;
        if (signalFilter === "low_retrieval") return t.low_retrieval_count > 0;
        if (signalFilter === "handoff") return t.handoff_count > 0;
        return true;
      });
    }

    // 2. Sort (stable — slice first to avoid mutating prop)
    const sorted = filtered.slice().sort((a, b) => {
      if (sortKey === "asks") return b.ask_count - a.ask_count;
      if (sortKey === "recent") return b.recent_count - a.recent_count;
      // last_seen: ISO strings compare lexicographically
      if (sortKey === "last_seen")
        return b.last_seen.localeCompare(a.last_seen);
      return 0;
    });

    return sorted;
  }, [themes, sortKey, signalFilter]);

  function toggleExpand(normQ: string) {
    setExpandedNormQs((prev) => {
      const next = new Set(prev);
      if (next.has(normQ)) {
        next.delete(normQ);
      } else {
        next.add(normQ);
      }
      return next;
    });
  }

  return (
    <div className="space-y-4">
      {/* Controls row */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Sort</span>
          <Select
            value={sortKey}
            onValueChange={(v) => setSortKey(v as SortKey)}
          >
            <SelectTrigger className="h-8 w-36 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="asks">Most asked</SelectItem>
              <SelectItem value="recent">Recently rising</SelectItem>
              <SelectItem value="last_seen">Last seen</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Signal</span>
          <Select
            value={signalFilter}
            onValueChange={(v) => setSignalFilter(v as SignalFilter)}
          >
            <SelectTrigger className="h-8 w-40 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All signals</SelectItem>
              <SelectItem value="no_retrieval">No answer found</SelectItem>
              <SelectItem value="low_retrieval">Weak match</SelectItem>
              <SelectItem value="handoff">Sent to human</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Sub-empty state when the filter excludes everything */}
      {displayedThemes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No themes match this filter.
        </p>
      ) : (
        <ul className="space-y-3">
          {displayedThemes.map((theme, i) => {
            // Expand state is keyed by the stable norm_q string, so the right
            // card stays open across sort/filter reorders. Fall back to the
            // example text + display index only if norm_q is ever blank.
            const expandKey = theme.norm_q || `${theme.example_question}-${i}`;
            const isExpanded = expandedNormQs.has(expandKey);

            return (
              <li key={expandKey}>
                <Card>
                  <CardContent className="flex flex-col gap-2 py-4">
                    <div className="flex items-start justify-between gap-4">
                      <button
                        type="button"
                        className="text-left text-sm font-medium hover:underline focus:outline-none focus-visible:underline"
                        onClick={() => toggleExpand(expandKey)}
                        aria-expanded={isExpanded}
                      >
                        &ldquo;{theme.example_question}&rdquo;
                      </button>
                      <div className="flex shrink-0 items-center gap-2">
                        {isRising(theme) ? (
                          <span className="rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
                            ↑ rising
                          </span>
                        ) : null}
                        <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold">
                          {theme.ask_count}{" "}
                          {theme.ask_count === 1 ? "ask" : "asks"}
                        </span>
                        <button
                          type="button"
                          onClick={() => toggleExpand(expandKey)}
                          className="rounded-sm px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                          aria-label={
                            isExpanded
                              ? "Collapse conversations"
                              : "Show conversations"
                          }
                        >
                          {isExpanded ? "▲ hide" : "▼ show"}
                        </button>
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {signalLine(theme)}
                    </p>

                    {/* Drill-down — lazy fetch on expand */}
                    {isExpanded && theme.norm_q ? (
                      <ThemeExamples
                        normQ={theme.norm_q}
                        windowDays={windowDays}
                      />
                    ) : null}
                  </CardContent>
                </Card>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
