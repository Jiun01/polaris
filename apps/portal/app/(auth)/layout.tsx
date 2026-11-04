/**
 * (auth) route-group layout (Phase 8 PR-2).
 *
 * No sidebar, no topbar — just the brand mark and a centered card.
 * Used for login, magic-link verify-request, and the (post-PR-2) error
 * page. Tailwind classes match the brand tokens established later in
 * PR-3 (calm, trustworthy, Linear/Notion adjacent).
 */

import type { ReactNode } from "react";

import { Logo } from "@/components/shell/Logo";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-background px-4 py-12">
      <div className="mb-8 flex flex-col items-center">
        <Logo className="h-8" />
      </div>
      <div className="w-full max-w-md">{children}</div>
    </main>
  );
}
