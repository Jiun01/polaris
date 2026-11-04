/**
 * DSAR export status / request page (Phase 8 PR-4).
 *
 * Per 00c §13.1: tenants request an export, we mail a signed download
 * URL within 24h, link expires 24h after issue. DSAR-on-expired-data
 * UX returns a manifest with `records_found: 0` + a tier-retention
 * explainer rather than an error.
 *
 * This page is the entry surface. The backend endpoint
 * `/api/tenant/privacy/export` is a Phase 1 deliverable; we POST to it
 * from the client.
 */

import Link from "next/link";

import { Button } from "@/components/ui/button";
import { DsarExportClient } from "@/components/settings/DsarExportClient";

export default function DsarExportPage() {
  return (
    <div className="space-y-6">
      <header>
        <Button asChild variant="ghost" size="sm" className="-ml-2">
          <Link href="/settings">&larr; Back to Settings</Link>
        </Button>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Export your data
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          PDPA / GDPR data-subject access request. We&apos;ll bundle a JSON
          archive of every record we hold for your tenant and email you a signed
          download link.
        </p>
      </header>

      <section className="rounded-xl bg-card p-6 outline outline-1 -outline-offset-1 outline-border">
        <h2 className="text-lg font-semibold">What you&apos;ll get</h2>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          <li>
            Conversations + messages within your retention window (
            <em>set by plan tier</em>).
          </li>
          <li>
            Audience members (opted-in + opted-out), templates, broadcasts.
          </li>
          <li>
            Knowledge documents + chunk metadata (raw uploaded files included).
          </li>
          <li>Invoices + billing history.</li>
        </ul>
        <p className="mt-3 text-xs text-muted-foreground">
          Audit logs are retained indefinitely (hash-chain) for compliance and
          are not included in the tenant-facing export.
        </p>
      </section>

      <section className="rounded-xl bg-card p-6 outline outline-1 -outline-offset-1 outline-border">
        <h2 className="text-lg font-semibold">Request export</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          We&apos;ll email a signed download link within 24 hours of submission.
          The link itself expires 24 hours after issue.
        </p>
        <div className="mt-4">
          <DsarExportClient />
        </div>
      </section>

      <p className="text-xs text-muted-foreground">
        Operating outside Malaysia or the EU and have a different DSAR
        requirement?{" "}
        <a href="mailto:billing@getpolaris.my" className="underline">
          Contact billing
        </a>
        .
      </p>
    </div>
  );
}
