/**
 * DeleteEaAccountButton — deprovisions a tenant's Easy!Appointments account.
 *
 * Destructive, so it follows the HardDeleteButton pattern: an idle button that
 * opens an inline confirm panel (type the slug to unlock), then DELETE via
 * adminFetch → toast → router.refresh.
 */

"use client";

import { useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface Props {
  tenantId: string;
  tenantSlug: string;
}

export function DeleteEaAccountButton({ tenantId, tenantSlug }: Props) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [open, setOpen] = useState(false);
  const [slugInput, setSlugInput] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const confirmed = slugInput === tenantSlug;

  function openPanel() {
    setOpen(true);
    setSlugInput("");
    setErrorMsg(null);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  async function handleDelete() {
    if (!confirmed) return;
    setErrorMsg(null);
    try {
      // The endpoint returns 200 even when EA is unreachable (failure_reason
      // set, status stays 'active'); surface that instead of a false success.
      const res = await adminFetch<{
        provisioned: boolean;
        failure_reason: string | null;
      }>(adminRoutes.tenants.deleteEaAccount(tenantId), { method: "DELETE" });
      if (res?.failure_reason) {
        setErrorMsg(
          `EA deprovision did not complete (${res.failure_reason}). Retry once EA is reachable.`,
        );
        return;
      }
      toast.success("EA account deprovisioned.");
      startTransition(() => {
        router.refresh();
      });
      setOpen(false);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : "Unexpected error — EA deprovision failed.";
      setErrorMsg(msg);
    }
  }

  if (!open) {
    return (
      <Button
        type="button"
        variant="destructive"
        size="sm"
        onClick={openPanel}
        data-testid="delete-ea-account-button"
      >
        Delete EA account
      </Button>
    );
  }

  return (
    <div className="space-y-3 rounded-md border border-destructive/40 bg-destructive/5 p-4">
      <p className="text-sm text-muted-foreground">
        This removes the tenant&apos;s EA admin, provider and service. Type{" "}
        <span className="font-mono font-semibold">{tenantSlug}</span> to
        confirm.
      </p>
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Label htmlFor="delete-ea-slug-input" className="sr-only">
            Confirm tenant slug
          </Label>
          <Input
            id="delete-ea-slug-input"
            ref={inputRef}
            value={slugInput}
            onChange={(e) => setSlugInput(e.target.value)}
            placeholder={tenantSlug}
            disabled={isPending}
            data-testid="delete-ea-slug-input"
          />
        </div>
        <Button
          type="button"
          variant="destructive"
          size="sm"
          disabled={!confirmed || isPending}
          onClick={handleDelete}
          data-testid="delete-ea-confirm"
        >
          {isPending ? "Deleting…" : "Delete EA account"}
        </Button>
        <Button
          type="button"
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
