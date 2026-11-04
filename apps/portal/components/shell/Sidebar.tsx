"use client";

/**
 * Portal left-rail navigation (Phase 8 PR-3).
 *
 * Desktop-only (md:flex+). Mobile uses <MobileNav> opened from the
 * topbar hamburger. Item catalogue lives in `nav-items.tsx` so both
 * surfaces stay in sync.
 *
 * Active state derives from the current pathname; we use startsWith
 * so a nested route like `/audience/opted-out` keeps Audience
 * highlighted.
 */

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Lock } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Logo } from "@/components/shell/Logo";
import { NAV_ITEMS, isNavItemActive } from "@/components/shell/nav-items";
import { useTenantContext } from "@/components/providers/tenant-provider";
import { meetsTier } from "@/lib/tier-gate";
import { cn } from "@/lib/utils";

export function Sidebar() {
  const pathname = usePathname() ?? "/";
  const { plan_tier } = useTenantContext();

  return (
    <aside className="hidden w-60 shrink-0 border-r border-border bg-card md:flex md:flex-col">
      <div className="flex h-14 items-center border-b border-border px-6">
        <Link
          href="/dashboard"
          className="flex items-center"
          aria-label="Polaris"
        >
          <Logo />
        </Link>
      </div>
      <nav
        className="flex-1 overflow-y-auto py-3"
        aria-label="Primary navigation"
      >
        <ul className="space-y-0.5 px-2">
          {NAV_ITEMS.map((item) => {
            const active = isNavItemActive(pathname, item.href);
            const locked =
              item.minTier !== undefined && !meetsTier(plan_tier, item.minTier);
            const Icon = item.icon;
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "group flex items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors",
                    active
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                    locked && "opacity-70",
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" aria-hidden />
                  <span className="flex-1 truncate">{item.label}</span>
                  {locked ? (
                    <Lock
                      className="h-3.5 w-3.5 text-muted-foreground"
                      role="img"
                      aria-label="Upgrade required"
                    />
                  ) : null}
                  {item.hint ? (
                    <Badge
                      variant="outline"
                      className="text-[10px] font-normal"
                    >
                      {item.hint}
                    </Badge>
                  ) : null}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="border-t border-border px-6 py-3 text-[11px] text-muted-foreground">
        v2 GA build
      </div>
    </aside>
  );
}
