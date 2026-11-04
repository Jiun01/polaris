/**
 * (admin) route-group layout (Phase 9 PR-3).
 *
 * Wraps every authenticated screen with the topbar + sidebar shell.
 * Middleware gates the route group — by the time this layout renders,
 * the request has a valid platform_admin session.
 *
 * Layout shape mirrors the wireframe brief (operator-grade density):
 *
 *   ┌──────────────────────────────────────────────┐
 *   │ TopBar (brand + env badge + email + sign-out)│
 *   ├────────┬─────────────────────────────────────┤
 *   │        │                                     │
 *   │ Sidebar│ <children>                          │
 *   │ (4grp) │                                     │
 *   │        │                                     │
 *   └────────┴─────────────────────────────────────┘
 */

import type { ReactNode } from "react";

import { Sidebar } from "@/components/shell/Sidebar";
import { TopBar } from "@/components/shell/TopBar";
import { ImpersonationBanner } from "@/components/shell/ImpersonationBanner";

export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen flex-col bg-background text-foreground">
      <ImpersonationBanner />
      <TopBar />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <main className="flex-1 overflow-y-auto bg-muted/30 p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
