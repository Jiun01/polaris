"use client";

/**
 * RemoveChannelButton — removes a single channel from a tenant.
 *
 * The inverse of AddChannelModal: deletes the tenant_channels row and its
 * captured outbound-token secret (server-side, transactional) so an admin can
 * reconfigure one channel without soft-deleting + rebuilding the whole tenant.
 *
 * Same three-state flow as SoftDeleteButton (idle → confirming → loading),
 * then router.refresh() so the parent RSC re-renders the channel list.
 *
 * Does NOT touch the Chatwoot inbox or the Meta/WABA webhook registration —
 * those live in Chatwoot/Meta, not Polaris.
 */

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface RemoveChannelButtonProps {
  tenantId: string;
  channelKind: string;
  /** Display label (e.g. "WhatsApp") for the confirm + toast copy. */
  channelLabel: string;
  /** Override checkbox/destructive actions are blocked during impersonation. */
  isImpersonating: boolean;
}

export function RemoveChannelButton({
  tenantId,
  channelKind,
  channelLabel,
  isImpersonating,
}: RemoveChannelButtonProps) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [confirming, setConfirming] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleRemove() {
    setIsSubmitting(true);
    try {
      await adminFetch(
        adminRoutes.tenants.removeChannel(tenantId, channelKind),
        { method: "DELETE" },
      );
      toast.success(`${channelLabel} channel removed.`);
      startTransition(() => {
        router.refresh();
      });
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Remove failed: ${err.message}`
          : "Unexpected error — remove failed.";
      toast.error(msg);
    } finally {
      setIsSubmitting(false);
      setConfirming(false);
    }
  }

  if (isImpersonating) return null;

  if (confirming) {
    return (
      <div className="flex items-center gap-2">
        <p className="text-xs text-muted-foreground">
          Remove {channelLabel}? Re-add it anytime.
        </p>
        <Button
          variant="destructive"
          size="sm"
          disabled={isSubmitting}
          onClick={handleRemove}
        >
          {isSubmitting ? "Removing…" : "Yes, remove"}
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
      variant="outline"
      size="sm"
      onClick={() => setConfirming(true)}
      data-testid={`remove-channel-${channelKind}`}
    >
      Remove
    </Button>
  );
}
