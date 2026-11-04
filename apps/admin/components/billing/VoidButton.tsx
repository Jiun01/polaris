"use client";

/**
 * Void button (v2.1).
 *
 * Calls POST /api/admin/billing/invoices/{id}/void with a required reason
 * and refreshes the server component tree on success. Voiding is for
 * issued/overdue invoices only; a paid invoice returns 409 (issue a
 * credit instead) and the error is surfaced inline.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface Props {
  invoiceId: string;
  /** Optional callback fired after a successful void. */
  onSuccess?: () => void;
}

export function VoidButton({ invoiceId, onSuccess }: Props) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleConfirm() {
    if (isPending) return;
    const trimmed = reason.trim();
    if (!trimmed) {
      setError("A reason is required to void.");
      return;
    }
    setIsPending(true);
    setError(null);
    try {
      await adminFetch<unknown>(adminRoutes.billing.voidInvoice(invoiceId), {
        method: "POST",
        body: { void_reason: trimmed },
      });
      onSuccess?.();
      setOpen(false);
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

  if (!open) {
    return (
      <Button
        variant="outline"
        size="sm"
        className="h-6 px-2 text-xs text-destructive"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
      >
        Void
      </Button>
    );
  }

  return (
    <span className="inline-flex flex-col gap-1">
      <span className="inline-flex items-center gap-1">
        <input
          type="text"
          autoFocus
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (required)"
          maxLength={500}
          className="h-6 w-44 rounded border border-border bg-background px-2 text-xs"
        />
        <Button
          variant="outline"
          size="sm"
          className="h-6 px-2 text-xs text-destructive"
          disabled={isPending}
          onClick={handleConfirm}
        >
          {isPending ? "Voiding…" : "Confirm"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs"
          disabled={isPending}
          onClick={() => {
            setOpen(false);
            setReason("");
            setError(null);
          }}
        >
          Cancel
        </Button>
      </span>
      {error && (
        <span className="text-xs text-destructive leading-tight">{error}</span>
      )}
    </span>
  );
}
