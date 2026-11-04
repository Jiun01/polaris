/**
 * SuspendButton — client component for suspend / unsuspend actions.
 *
 * The LifecycleTab server component used a bare <form method="post"> that
 * posted to a non-existent Next.js route, resulting in a 404. This component
 * routes the action through the same-origin proxy (adminFetch) so it reaches
 * FastAPI correctly, then refreshes the RSC tree on success.
 */

"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface Props {
  tenantId: string;
  suspended: boolean;
}

export function SuspendButton({ tenantId, suspended }: Props) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [loading, setLoading] = useState(false);

  const busy = isPending || loading;

  async function handleClick() {
    setLoading(true);
    try {
      const path = suspended
        ? adminRoutes.tenants.unsuspend(tenantId)
        : adminRoutes.tenants.suspend(tenantId);
      await adminFetch(path, { method: "POST" });
      toast.success(suspended ? "Tenant unsuspended." : "Tenant suspended.");
      startTransition(() => {
        router.refresh();
      });
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : suspended
            ? "Failed to unsuspend tenant."
            : "Failed to suspend tenant.";
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Button
      type="button"
      variant={suspended ? "default" : "outline"}
      disabled={busy}
      onClick={handleClick}
    >
      {busy
        ? suspended
          ? "Unsuspending…"
          : "Suspending…"
        : suspended
          ? "Unsuspend"
          : "Suspend"}
    </Button>
  );
}
