"use client";

/**
 * In-page tab navigation for the Knowledge screen (Phase 8 PR-6).
 *
 * Three tabs map 1:1 to routes so deep-linking + browser back behave
 * naturally:
 *
 *   - Sources  →  /knowledge          (default)
 *   - Runs     →  /knowledge/runs
 *
 * The Chunks tab is intentionally absent from the tab row — chunk
 * inspection is a per-source drill-down reached by clicking a source
 * row, not a global view. portal_design_prompt.md §3 notes it's
 * advanced + rarely used so the navigation surface stays slim.
 *
 * Client component because `usePathname` is the cleanest way to
 * derive active state without prop-drilling from each page.
 */

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const TABS = [
  { href: "/knowledge", label: "Sources" },
  { href: "/knowledge/runs", label: "Runs" },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/knowledge") {
    // Sources is active for the base path AND for the chunks drill-down
    // — the drill-down has no tab of its own and "returns to Sources".
    return (
      pathname === "/knowledge" || pathname.startsWith("/knowledge/chunks")
    );
  }
  return pathname === href;
}

export function KnowledgeTabs() {
  const pathname = usePathname() ?? "/knowledge";

  return (
    <nav className="border-b" aria-label="Knowledge sub-navigation">
      <ul className="-mb-px flex gap-4">
        {TABS.map((tab) => {
          const active = isActive(pathname, tab.href);
          return (
            <li key={tab.href}>
              <Link
                href={tab.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "inline-block border-b-2 px-1 pb-2.5 pt-1 text-sm font-medium transition-colors",
                  active
                    ? "border-foreground text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {tab.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
