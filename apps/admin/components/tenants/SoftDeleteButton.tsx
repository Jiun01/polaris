"use client";

/**
 * SoftDeleteButton — flips a tenant to status='deleted'.
 *
 * Three-state flow:
 *   idle → confirming ("Stop all operations and lock portal. Continue?")
 *         → loading (spinner)
 *         → done (toast + router.refresh)
 *
 * The parent RSC re-renders after router.refresh() so the status badge and
 * Lifecycle card update without a full page reload.
 */

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface SoftDeleteButtonProps {
  tenantId: string;
  tenantSlug: string;
}

export function SoftDeleteButton({
  tenantId,
  tenantSlug,
}: SoftDeleteButtonProps) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [confirming, setConfirming] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleDelete() {
    setIsSubmitting(true);
    try {
      await adminFetch(adminRoutes.tenants.softDelete(tenantId), {
        method: "DELETE",
      });
      toast.success(
        `"${tenantSlug}" soft-deleted — portal locked, bot stopped.`,
      );
      startTransition(() => {
        router.refresh();
      });
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Delete failed: ${err.message}`
          : "Unexpected error — delete failed.";
      toast.error(msg);
    } finally {
      setIsSubmitting(false);
      setConfirming(false);
    }
  }

  if (confirming) {
    return (
      <div className="flex items-center gap-2">
        <p className="text-sm text-muted-foreground">
          Stop all operations and lock the portal. Continue?
        </p>
        <Button
          variant="destructive"
          size="sm"
          disabled={isSubmitting}
          onClick={handleDelete}
        >
          {isSubmitting ? "Deleting…" : "Yes, soft-delete"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={isSubmitting}
          onClick={() => setConfirming(false)}
        >
          Cancel
        </Button>
      </div>
    );
  }

  return (
    <Button
      variant="destructive"
      size="sm"
      onClick={() => setConfirming(true)}
      data-testid="soft-delete-button"
    >
      Soft Delete
    </Button>
  );
}
