/**
 * Shared sidebar navigation catalogue (Phase 8 PR-3).
 *
 * Both `Sidebar.tsx` (desktop left rail) and `MobileNav.tsx` (md:hidden
 * sheet) render this list. Keeps the 16-item catalogue + tier locks +
 * hint badges in one place so a future nav add doesn't drift between
 * the two surfaces.
 */

import {
  BarChart3,
  Bot,
  Boxes,
  Brain,
  Calendar,
  GitBranch,
  Lightbulb,
  type LucideIcon,
  MapPin,
  Megaphone,
  MessageCircle,
  MessagesSquare,
  Package,
  Receipt,
  Settings,
  Tag,
  Users,
} from "lucide-react";

import type { PlanTier } from "@/lib/tier-gate";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Minimum tier required to USE this surface (locked variant rendered below). */
  minTier?: PlanTier;
  /** Permanent "coming in vX.Y" badge to surface a deferred screen. */
  hint?: string;
}

export const NAV_ITEMS: ReadonlyArray<NavItem> = [
  { href: "/dashboard", label: "Dashboard", icon: BarChart3 },
  { href: "/conversations", label: "Conversations", icon: MessageCircle },
  { href: "/agent", label: "AI Agent", icon: Bot },
  { href: "/knowledge", label: "Knowledge", icon: Brain },
  {
    href: "/knowledge-gaps",
    label: "Knowledge Gaps",
    icon: Lightbulb,
    minTier: "ultra",
  },
  { href: "/templates", label: "Templates", icon: Boxes },
  { href: "/audience", label: "Audience", icon: Users },
  { href: "/broadcasts", label: "Broadcasts", icon: Megaphone, minTier: "pro" },
  { href: "/channels", label: "Channels", icon: GitBranch },
  { href: "/widget", label: "Website Widget", icon: MessagesSquare },
  { href: "/bookings", label: "Bookings", icon: Calendar },
  { href: "/promotions", label: "Promotions", icon: Tag },
  { href: "/products", label: "Products", icon: Package },
  { href: "/branches", label: "Branches", icon: MapPin, minTier: "pro" },
  { href: "/billing", label: "Billing & Usage", icon: Receipt },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function isNavItemActive(pathname: string, href: string): boolean {
  if (href === "/dashboard") return pathname === "/dashboard";
  return pathname === href || pathname.startsWith(`${href}/`);
}
