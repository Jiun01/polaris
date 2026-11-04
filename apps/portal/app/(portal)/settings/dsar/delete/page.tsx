/**
 * Account-deletion explainer (Phase 8 PR-4).
 *
 * Per ADR-029: tenant_admin requests via portal Settings → platform_admin
 * approves in admin app → soft-delete fires at end of current paid
 * period → 30-day recovery window → hard purge.
 *
 * The actual submit lives in the Settings page's AccountDeletionDialog
 * (full consequences + confirmation checkbox before POST). This route
 * exists so the legalese can carry a stable URL we can link from emails
 * or support replies.
 */

import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function DsarDeletePage() {
  return (
    <div className="space-y-6">
      <header>
        <Button asChild variant="ghost" size="sm" className="-ml-2">
          <Link href="/settings">&larr; Back to Settings</Link>
        </Button>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Delete your account
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Per ADR-029, deletions follow a 30-day grace window so accidental
          requests stay recoverable. Here&apos;s what happens after you submit.
        </p>
      </header>

      <section className="rounded-xl bg-card p-6 outline outline-1 -outline-offset-1 outline-border">
        <h2 className="text-lg font-semibold">Timeline</h2>
        <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm text-muted-foreground">
          <li>
            <strong>Today.</strong> You submit a deletion request from Settings
            &gt; Account.
          </li>
          <li>
            <strong>End of current paid period.</strong> We soft-delete your
            tenant: bot stops replying, messaging inbox is disabled, portal
            access is locked. You keep the service you paid for until the period
            ends.
          </li>
          <li>
            <strong>Up to 30 days after soft-delete.</strong> A support request
            restores everything. After 30 days, restoration is no longer
            possible.
          </li>
          <li>
            <strong>Day 30 hard-purge.</strong> Conversations, knowledge,
            broadcasts, audience members, and secrets. All unrecoverable. Audit
            logs are retained 7 years for compliance.
          </li>
        </ol>
      </section>

      <section className="rounded-xl bg-card p-6 outline outline-1 -outline-offset-1 outline-border">
        <h2 className="text-lg font-semibold">Before you delete</h2>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          <li>
            Run a{" "}
            <Link href="/settings/dsar/export" className="underline">
              data export
            </Link>{" "}
            if you want your records.
          </li>
          <li>
            Cancel any scheduled broadcasts so they don&apos;t fire after
            soft-delete.
          </li>
          <li>
            Tell your customers a wind-down date so the silent-bot experience
            doesn&apos;t come as a surprise.
          </li>
        </ul>
      </section>

      <section className="rounded-xl bg-card p-6 outline outline-1 -outline-offset-1 outline-destructive/40">
        <h2 className="text-lg font-semibold text-destructive">
          Ready to proceed?
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Submit the request from Settings &gt; Account, and you&apos;ll get a
          confirmation dialog with a checkbox before anything fires.
        </p>
        <div className="mt-4">
          <Button asChild variant="outline">
            <Link href="/settings">Back to Settings &gt; Account</Link>
          </Button>
        </div>
      </section>
    </div>
  );
}
