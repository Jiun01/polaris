"use client";

/**
 * Mark-paid button (Phase 9 PR-7).
 *
 * Calls POST /api/admin/billing/invoices/{id}/mark-paid and refreshes the
 * server component tree on success via router.refresh().
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface Props {
  invoiceId: string;
  /** Optional callback fired after a successful mark-paid. */
  onSuccess?: () => void;
}

export function MarkPaidButton({ invoiceId, onSuccess }: Props) {
  const router = useRouter();
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    if (isPending) return;
    setIsPending(true);
    setError(null);
    try {
      await adminFetch<unknown>(adminRoutes.billing.markPaid(invoiceId), {
        method: "POST",
      });
      onSuccess?.();
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Unexpected error");
      }
    } finally {
      setIsPending(false);
    }
  }

  return (
    <span className="inline-flex flex-col gap-0.5">
      <Button
        variant="outline"
        size="sm"
        className="h-6 px-2 text-xs"
        disabled={isPending}
        onClick={handleClick}
      >
        {isPending ? "Saving…" : "Mark Paid"}
      </Button>
      {error && (
        <span className="text-xs text-destructive leading-tight">{error}</span>
      )}
    </span>
  );
}
