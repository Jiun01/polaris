/**
 * Admin sidebar (Phase 9 PR-3).
 *
 * 4 nav groups per the wireframe brief
 * (_artifacts/admin_dashboard_wireframe_prompt.md §"Global layout"):
 *
 *   Configure  → Tenants, Quotas, Secrets, Promotions, Channels
 *   Monitor    → System Health, Cost, Financials
 *   Operate    → Billing, Plan Changes, Quotes, Audit Log
 *   Compliance → Impersonation
 *
 * Active route is highlighted by reading the pathname client-side
 * (small footprint client component; the rest of the shell stays
 * server-rendered).
 *
 * PR-3 ships Tenants + the read-only Promotions/Channels mirrors. The
 * remaining items render as links that point at routes which 404 until
 * their owner PR lands — intentional: the operator can scout the
 * intended nav surface from day one.
 */

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Building2,
  GaugeCircle,
  KeyRound,
  Megaphone,
  MessagesSquare,
  Presentation,
  HeartPulse,
  TrendingUp,
  Wallet,
  Receipt,
  ArrowRightLeft,
  FileSignature,
  ScrollText,
  ShieldAlert,
  Inbox,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const NAV: NavGroup[] = [
  {
    label: "Configure",
    items: [
      { href: "/tenants", label: "Tenants", icon: Building2 },
      { href: "/demo-system", label: "Demo System", icon: Presentation },
      { href: "/quotas", label: "Quotas", icon: GaugeCircle },
      { href: "/secrets", label: "Secrets", icon: KeyRound },
      { href: "/promotions", label: "Promotions", icon: Megaphone },
      { href: "/channels", label: "Channels", icon: MessagesSquare },
    ],
  },
  {
    label: "Monitor",
    items: [
      { href: "/health", label: "System Health", icon: HeartPulse },
      { href: "/cost", label: "Cost", icon: Wallet },
      { href: "/financials", label: "Financials", icon: TrendingUp },
    ],
  },
  {
    label: "Operate",
    items: [
      { href: "/billing", label: "Billing", icon: Receipt },
      { href: "/plan-changes", label: "Plan Changes", icon: ArrowRightLeft },
      { href: "/quotes", label: "Sales Quotes", icon: FileSignature },
      { href: "/leads", label: "Website Leads", icon: Inbox },
      { href: "/audit", label: "Audit Log", icon: ScrollText },
    ],
  },
  {
    label: "Compliance",
    items: [
      {
        href: "/impersonation",
        label: "Impersonation",
        icon: ShieldAlert,
      },
    ],
  },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Sidebar() {
  const pathname = usePathname();
  return (
    <nav
      className="flex h-full w-56 shrink-0 flex-col border-r border-border bg-background py-4"
      aria-label="Admin navigation"
    >
      <div className="flex-1 space-y-6 overflow-y-auto px-3">
        {NAV.map((group) => (
          <div key={group.label}>
            <p
              id={`nav-group-${group.label.toLowerCase()}`}
              className="mb-1.5 px-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground"
            >
              {group.label}
            </p>
            <ul
              aria-labelledby={`nav-group-${group.label.toLowerCase()}`}
              className="space-y-0.5"
            >
              {group.items.map((item) => {
                const Icon = item.icon;
                const active = isActive(pathname, item.href);
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      className={cn(
                        "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium transition",
                        active
                          ? "bg-secondary text-secondary-foreground"
                          : "text-muted-foreground hover:bg-muted hover:text-foreground",
                      )}
                      aria-current={active ? "page" : undefined}
                    >
                      <Icon className="h-4 w-4" aria-hidden />
                      {item.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  );
}
