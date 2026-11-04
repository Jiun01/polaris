"use client";

/**
 * HardDeleteButton — permanently purges a soft-deleted tenant.
 *
 * The backend requires status='deleted' first (409 otherwise).
 * This component enforces that invariant at the UI layer by disabling
 * the button when tenantStatus !== 'deleted'.
 *
 * Confirmation flow:
 *   idle → inline panel (type slug to unlock submit)
 *         → loading → redirect to /tenants on 204
 *         → inline error on failure (no redirect)
 *
 * Typing the slug as confirmation prevents accidental purge of the wrong
 * tenant when admins have multiple tabs open.
 */

import { useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface HardDeleteButtonProps {
  tenantId: string;
  tenantSlug: string;
  tenantStatus: string;
}

export function HardDeleteButton({
  tenantId,
  tenantSlug,
  tenantStatus,
}: HardDeleteButtonProps) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [open, setOpen] = useState(false);
  const [slugInput, setSlugInput] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const isSoftDeleted = tenantStatus === "deleted";
  const confirmed = slugInput === tenantSlug;

  function openPanel() {
    setOpen(true);
    setSlugInput("");
    setErrorMsg(null);
    // Focus the input on the next paint after the panel mounts.
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  async function handlePurge() {
    if (!confirmed) return;
    setErrorMsg(null);
    try {
      await adminFetch(adminRoutes.tenants.purge(tenantId), {
        method: "DELETE",
      });
      toast.success(`"${tenantSlug}" permanently deleted.`);
      startTransition(() => {
        router.push("/tenants");
      });
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : "Unexpected error — purge failed.";
      setErrorMsg(msg);
    }
  }

  if (!open) {
    return (
      <Button
        variant="destructive"
        size="sm"
        disabled={!isSoftDeleted}
        onClick={openPanel}
        title={
          isSoftDeleted
            ? undefined
            : "Tenant must be soft-deleted before permanent purge"
        }
        data-testid="hard-delete-button"
      >
        Permanently Delete
      </Button>
    );
  }

  return (
    <div className="space-y-3 rounded-md border border-destructive/40 bg-destructive/5 p-4">
      <p className="text-sm font-medium text-destructive">
        This action is irreversible. All data — conversations, secrets, audit
        logs, invoices — will be permanently destroyed.
      </p>
      <p className="text-sm text-muted-foreground">
        Type <span className="font-mono font-semibold">{tenantSlug}</span> to
        confirm.
      </p>
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Label htmlFor="purge-slug-input" className="sr-only">
            Confirm tenant slug
          </Label>
          <Input
            id="purge-slug-input"
            ref={inputRef}
            value={slugInput}
            onChange={(e) => setSlugInput(e.target.value)}
            placeholder={tenantSlug}
            disabled={isPending}
            data-testid="hard-delete-slug-input"
          />
        </div>
        <Button
          variant="destructive"
          size="sm"
          disabled={!confirmed || isPending}
          onClick={handlePurge}
          data-testid="hard-delete-confirm"
        >
          {isPending ? "Deleting…" : "Permanently Delete"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={isPending}
          onClick={() => setOpen(false)}
        >
          Cancel
        </Button>
      </div>
      {errorMsg && (
        <p className="text-xs text-destructive" role="alert">
          {errorMsg}
        </p>
      )}
    </div>
  );
}
