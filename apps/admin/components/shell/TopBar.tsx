/**
 * Admin topbar (Phase 9 PR-3).
 *
 * Slim chrome: "Polaris Admin" brand on the left, environment badge +
 * platform-admin email + sign-out on the right. Per the wireframe
 * brief — minimal ceremony, dense data, no marketing flourish.
 *
 * Server component — reads the session via getServerSession. The
 * sign-out button posts to NextAuth's /api/auth/signout endpoint via
 * a plain form so this stays a Server Component.
 */

import { getServerSession } from "next-auth/next";

import { authOptions } from "@/lib/auth/options";
import { EnvironmentBadge } from "@/components/shell/EnvironmentBadge";

export async function TopBar() {
  const session = await getServerSession(authOptions);
  const email = session?.user?.email ?? "—";

  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-background px-4">
      <div className="flex items-center gap-3">
        <p className="text-sm font-semibold tracking-tight text-foreground">
          Polaris Admin
        </p>
        <EnvironmentBadge />
      </div>
      <div className="flex items-center gap-3">
        <p className="hidden text-xs text-muted-foreground sm:block">{email}</p>
        <form method="post" action="/api/auth/signout">
          <input type="hidden" name="callbackUrl" value="/login" />
          <button
            type="submit"
            className="rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium text-foreground transition hover:bg-muted"
          >
            Sign out
          </button>
        </form>
      </div>
    </header>
  );
}
