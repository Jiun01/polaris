/**
 * Starter-tier Branches placeholder (Phase 8 PR-9).
 *
 * Read-only "HQ" card + Pro upgrade CTA. ADR-041 Amendment: never
 * hide the feature — show the upgrade path inline.
 */

import Link from "next/link";
import { Lock, MapPin } from "lucide-react";

import { Button } from "@/components/ui/button";

export function BranchesStarterPlaceholder() {
  return (
    <div className="space-y-4">
      <div className="rounded-xl border bg-card p-4 opacity-75">
        <div className="flex items-center gap-2">
          <MapPin className="h-4 w-4 text-muted-foreground" aria-hidden />
          <h3 className="font-semibold">HQ</h3>
          <span className="text-xs text-muted-foreground">
            (your tenant, implicit)
          </span>
        </div>
        <p className="mt-2 text-sm text-muted-foreground">
          Starter plan supports a single location. Upgrade to Pro to add named
          branches with per-branch knowledge, audiences, and booking config.
        </p>
      </div>

      <div className="rounded-xl border bg-card p-6 text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <Lock className="h-5 w-5" aria-hidden />
        </div>
        <h2 className="mt-3 text-base font-semibold">
          Multi-branch is a Pro feature
        </h2>
        <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
          Run more than one storefront, office, or franchise location? Pro lets
          you tag every contact, source, and booking per branch so the bot
          routes correctly.
        </p>
        <div className="mt-4">
          <Button asChild>
            <Link href="/billing/plan-change?target=pro">Upgrade to Pro</Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
