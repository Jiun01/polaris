"use client";

/**
 * CSV-import + sync-from-conversations client form (Phase 8 PR-8).
 *
 * Two affordances on one page:
 *   - File upload to `/api/tenant/audience/import`
 *   - "Sync inbound senders" CTA to `/api/tenant/audience/import-from-conversations`
 *
 * Both return a CSVImportResult; we surface imported / skipped / error
 * counts inline so the tenant immediately sees what happened.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

interface CSVImportResult {
  imported_count: number;
  skipped_count: number;
  error_rows: { row_number: number; reason: string }[];
}

export function AudienceImportClient() {
  const router = useRouter();
  const [file, setFile] = React.useState<File | null>(null);
  const [uploading, setUploading] = React.useState(false);
  const [syncing, setSyncing] = React.useState(false);
  const [result, setResult] = React.useState<CSVImportResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  async function uploadCsv() {
    if (!file) return;
    setUploading(true);
    setError(null);
    setResult(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const json = await apiFetch<CSVImportResult>(
        TenantRoutes.audienceImport(),
        { method: "POST", body: fd },
      );
      if (json) setResult(json);
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "CSV import failed. Check the file shape and try again.",
      );
    } finally {
      setUploading(false);
    }
  }

  async function syncFromConversations() {
    setSyncing(true);
    setError(null);
    setResult(null);
    try {
      const json = await apiFetch<CSVImportResult>(
        TenantRoutes.audienceImportFromConversations(30),
        { method: "POST" },
      );
      if (json) setResult(json);
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't sync inbound senders.",
      );
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <section className="rounded-xl border bg-card p-4">
        <h3 className="text-base font-semibold">Upload CSV</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Drop a CSV exported from your existing CRM or contact list.
        </p>
        <div className="mt-3 space-y-2">
          <Label htmlFor="csv-file">File</Label>
          <Input
            id="csv-file"
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
        <Button
          className="mt-3"
          onClick={uploadCsv}
          disabled={uploading || !file}
        >
          {uploading ? "Uploading…" : "Upload"}
        </Button>
      </section>

      <section className="rounded-xl border bg-card p-4">
        <h3 className="text-base font-semibold">Sync from inbound senders</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Add every customer who messaged your bot in the last 30 days.
          Already-imported and opted-out contacts are skipped automatically.
        </p>
        <Button
          className="mt-3"
          variant="outline"
          onClick={syncFromConversations}
          disabled={syncing}
        >
          {syncing ? "Syncing…" : "Sync recent inbounds"}
        </Button>
      </section>

      {result ? (
        <div
          role="status"
          className="col-span-full rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm dark:border-emerald-900 dark:bg-emerald-950"
        >
          <strong className="font-semibold">Done.</strong> Imported{" "}
          {result.imported_count}, skipped {result.skipped_count}
          {result.error_rows.length > 0
            ? `, ${result.error_rows.length} row${result.error_rows.length === 1 ? "" : "s"} had issues.`
            : "."}
          {result.error_rows.length > 0 ? (
            <details className="mt-2 text-xs text-muted-foreground">
              <summary className="cursor-pointer">Show errors</summary>
              <ul className="mt-1 list-disc pl-5">
                {result.error_rows.slice(0, 20).map((r) => (
                  <li key={r.row_number}>
                    Row {r.row_number}: {r.reason}
                  </li>
                ))}
                {result.error_rows.length > 20 ? (
                  <li>…and {result.error_rows.length - 20} more.</li>
                ) : null}
              </ul>
            </details>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="col-span-full text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}
