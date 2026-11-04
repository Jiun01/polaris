/**
 * Audience — Import tab (Phase 8 PR-8).
 *
 * Wraps the existing `POST /api/tenant/audience/import` (CSV upload)
 * + `POST /api/tenant/audience/import-from-conversations` endpoints
 * with a small client form. Two paths:
 *
 *   1. **Upload CSV** — file input. Columns documented inline.
 *   2. **Sync from recent inbounds** — bulk-insert opt-in candidates
 *      from the last 30 days of inbound messages.
 *
 * The page is rendered as a Server Component shell; the import client
 * carries the form state.
 */

import { AudienceImportClient } from "@/components/audience/AudienceImportClient";

export default function AudienceImportPage() {
  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">CSV columns</h2>
        <p className="text-sm text-muted-foreground">
          Required: <code className="rounded bg-muted px-1">channel</code>,{" "}
          <code className="rounded bg-muted px-1">channel_user_id</code>,{" "}
          <code className="rounded bg-muted px-1">opt_in_source</code>.
          Optional: <code className="rounded bg-muted px-1">phone_e164</code>,{" "}
          <code className="rounded bg-muted px-1">email</code>,{" "}
          <code className="rounded bg-muted px-1">name</code>,{" "}
          <code className="rounded bg-muted px-1">locale</code>,{" "}
          <code className="rounded bg-muted px-1">tags</code>,{" "}
          <code className="rounded bg-muted px-1">branch</code>.
        </p>
        <p className="text-xs text-muted-foreground">
          Up to 100,000 rows, 10 MB per upload. Rows for already-opted-in
          contacts are skipped; opted-out contacts are <strong>never</strong>{" "}
          re-imported (PDPA).
        </p>
      </section>

      <AudienceImportClient />
    </div>
  );
}
