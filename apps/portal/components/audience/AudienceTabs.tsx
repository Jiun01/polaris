"use client";

/**
 * Audience screen sub-navigation (Phase 8 PR-8).
 *
 * Mirrors the Knowledge tabs pattern from PR-6 — Server Component
 * layout, client child for `usePathname`-driven active state.
 */

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const TABS = [
  { href: "/audience", label: "Members" },
  { href: "/audience/opted-out", label: "Opted-out" },
  { href: "/audience/import", label: "Import" },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/audience") return pathname === "/audience";
  return pathname === href;
}

export function AudienceTabs() {
  const pathname = usePathname() ?? "/audience";
  return (
    <nav className="border-b" aria-label="Audience sub-navigation">
      <ul className="-mb-px flex gap-4">
        {TABS.map((t) => {
          const active = isActive(pathname, t.href);
          return (
            <li key={t.href}>
              <Link
                href={t.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "inline-block border-b-2 px-1 pb-2.5 pt-1 text-sm font-medium transition-colors",
                  active
                    ? "border-foreground text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
