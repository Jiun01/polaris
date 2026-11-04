/**
 * (suspended) route-group layout (Phase 8 PR-2).
 *
 * Used when the tenant's `suspended_reason='payment_overdue'` (ADR-049).
 * Middleware redirects every other route here; only the payment page
 * renders. No sidebar, no topbar, no notification bell — the tenant
 * cannot use any portal feature until they settle the invoice.
 *
 * A signed-in but quota-exhausted tenant does NOT land here — that
 * suspension keeps Chatwoot active and the portal usable (ADR-018);
 * only the bot is off. payment_overdue is the only state that locks
 * the full portal.
 */

import type { ReactNode } from "react";

import { Logo } from "@/components/shell/Logo";

export default function SuspendedLayout({ children }: { children: ReactNode }) {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-background px-4 py-12">
      <div className="mb-8 flex flex-col items-center">
        <Logo className="h-8" />
      </div>
      <div className="w-full max-w-lg">{children}</div>
      <p className="mt-8 text-xs text-muted-foreground">
        <a
          href="/api/auth/signout"
          className="underline underline-offset-4 hover:text-foreground"
        >
          Sign out
        </a>
      </p>
    </main>
  );
}
