"use client";

/**
 * Mark-all-read button for the full-page notifications view (Phase 8 PR-11).
 *
 * Refreshes the route after a successful mark so the read state is
 * visible without polling.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { apiFetch } from "@/lib/api/client";
import { TenantRoutes } from "@/lib/api/routes";

export function MarkAllReadButton() {
  const router = useRouter();
  const [submitting, setSubmitting] = React.useState(false);

  async function markAll() {
    setSubmitting(true);
    try {
      await apiFetch(TenantRoutes.notificationsMarkAllRead(), {
        method: "POST",
      });
      router.refresh();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Button variant="outline" onClick={markAll} disabled={submitting}>
      {submitting ? "Marking…" : "Mark all read"}
    </Button>
  );
}
