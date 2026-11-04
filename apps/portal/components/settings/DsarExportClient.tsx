"use client";

/**
 * DSAR export status + request client (Phase 8 PR-4).
 *
 * Drives the /settings/dsar/export page. Two states:
 *
 *   1. Idle — show the "Request export" button.
 *   2. Submitted — show a pending placeholder; the backend will email
 *      a signed-URL link within the 24h SLA per 00c §13.1.
 *
 * Polling the export status mid-session is intentionally deferred to a
 * follow-up — at GA the email-link delivery is the source of truth and
 * the portal doesn't need a live status table yet.
 */

import * as React from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

export function DsarExportClient() {
  const [submitting, setSubmitting] = React.useState(false);
  const [submitted, setSubmitted] = React.useState(false);

  async function request() {
    setSubmitting(true);
    try {
      await apiFetch<unknown>(TenantRoutes.privacyExport(), { method: "POST" });
      toast.success("Export requested. Check your inbox within 24h.");
      setSubmitted(true);
    } catch (err) {
      const msg =
        err instanceof PolarisApiError
          ? err.message
          : "We couldn't submit your request. Email billing@getpolaris.my.";
      toast.error(msg);
    } finally {
      setSubmitting(false);
    }
  }

  if (submitted) {
    return (
      <div className="rounded-lg border bg-card p-6">
        <h2 className="text-base font-semibold">Export pending</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          We&apos;ll email a signed download link to your account address within
          24 hours. Links expire 24h after issue.
        </p>
        <p className="mt-3 text-xs text-muted-foreground">
          If you don&apos;t receive it within a day, contact{" "}
          <a className="underline" href="mailto:billing@getpolaris.my">
            billing@getpolaris.my
          </a>
          .
        </p>
      </div>
    );
  }

  return (
    <Button onClick={request} disabled={submitting}>
      {submitting ? "Submitting…" : "Request data export"}
    </Button>
  );
}
