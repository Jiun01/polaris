/**
 * Broadcasts — locked variant (Phase 8 PR-9).
 *
 * Starter tenants land here when they hit /broadcasts. Renders the
 * upgrade card per ADR-026 Am.3 — "never hide the feature" + clear
 * upgrade CTA. Pro+ tenants visiting this route are bounced to the
 * real broadcasts list.
 */

import Link from "next/link";
import { Lock } from "lucide-react";

import { Button } from "@/components/ui/button";
import { TierUnlockRedirect } from "@/components/shell/TierUnlockRedirect";

export default function BroadcastsLockedPage() {
  return (
    <TierUnlockRedirect minTier="pro" unlockedPath="/broadcasts">
      <div className="space-y-6">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold tracking-tight">Broadcasts</h1>
          <p className="text-sm text-muted-foreground">
            Send approved WhatsApp templates to opted-in audience segments.
          </p>
        </header>

        <div className="rounded-xl border bg-card p-8 text-center">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
            <Lock className="h-5 w-5" aria-hidden />
          </div>
          <h2 className="mt-4 text-lg font-semibold">
            Broadcasts are a Pro feature
          </h2>
          <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
            Upgrade to Pro to send promotional WhatsApp messages to your
            opted-in audience, schedule campaigns, and track delivery / read
            rates.
          </p>
          <div className="mt-5 flex flex-col items-center gap-2">
            <Button asChild>
              <Link href="/billing/plan-change?target=pro">Upgrade to Pro</Link>
            </Button>
            <Link
              href="/billing"
              className="text-xs text-muted-foreground underline"
            >
              Compare plans
            </Link>
          </div>
        </div>
      </div>
    </TierUnlockRedirect>
  );
}
