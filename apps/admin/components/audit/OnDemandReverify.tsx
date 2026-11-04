/**
 * On-demand audit chain re-verification trigger button (Phase 9 PR-5).
 *
 * Client component — POSTs to /api/admin/audit/verify, then refreshes the
 * page so the VerificationBanner picks up the new result.
 */

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ShieldCheck } from "lucide-react";

import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { Button } from "@/components/ui/button";

export function OnDemandReverify() {
  const [pending, setPending] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const router = useRouter();

  async function handleClick() {
    if (pending) return;
    setPending(true);
    setMsg(null);
    try {
      const data = await adminFetch<{ status: string; message: string }>(
        adminRoutes.audit.triggerVerify(),
        { method: "POST" },
      );
      setMsg(data?.message ?? "Verification triggered.");
      // Refresh page after 3 s to pick up updated result.
      setTimeout(() => router.refresh(), 3000);
    } catch (err) {
      setMsg(`Error: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex items-center gap-3">
      <Button
        variant="outline"
        size="sm"
        onClick={handleClick}
        disabled={pending}
        className="gap-1.5"
      >
        <ShieldCheck className="h-4 w-4" aria-hidden />
        {pending ? "Running…" : "Verify now"}
      </Button>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="text-xs text-muted-foreground"
      >
        {msg ?? ""}
      </p>
    </div>
  );
}
