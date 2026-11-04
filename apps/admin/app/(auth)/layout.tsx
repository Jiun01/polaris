/**
 * (auth) route-group layout for admin (Phase 9 PR-2).
 *
 * Mirrors apps/portal/app/(auth)/layout.tsx; brand mark reads
 * "POLARIS ADMIN" so the operator can tell at a glance which app they're
 * authenticating to (vs. the public portal at portal.getpolaris.my).
 */

import type { ReactNode } from "react";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-slate-50 px-4 py-12 dark:bg-slate-950">
      <div className="mb-8 flex flex-col items-center gap-2">
        <p className="text-xs font-medium uppercase tracking-[0.18em] text-slate-500">
          Polaris Admin
        </p>
      </div>
      <div className="w-full max-w-md">{children}</div>
    </main>
  );
}
