"use client";

/**
 * ChatwootAccountField — inline edit for tenants.chatwoot_account_id.
 *
 * Shows the current value (or "Not configured" warning) with a pencil button
 * that opens an inline number input. Saves via PATCH /{tenant_id}/chatwoot-account
 * and calls router.refresh() so the server component re-fetches.
 */

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface ChatwootAccountFieldProps {
  tenantId: string;
  current: number | null;
}

export function ChatwootAccountField({
  tenantId,
  current,
}: ChatwootAccountFieldProps) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(current ? String(current) : "");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const parsed = parseInt(value, 10);
  const isValid = !isNaN(parsed) && parsed >= 1;

  async function handleSave() {
    if (!isValid) return;
    setIsSubmitting(true);
    setErrorMsg(null);
    try {
      await adminFetch(adminRoutes.tenants.chatwootAccount(tenantId), {
        method: "PATCH",
        body: { chatwoot_account_id: parsed },
      });
      toast.success(`Chatwoot account ID set to ${parsed}.`);
      setEditing(false);
      startTransition(() => router.refresh());
    } catch (err) {
      setErrorMsg(err instanceof ApiError ? err.message : "Update failed.");
    } finally {
      setIsSubmitting(false);
    }
  }

  if (!editing) {
    return (
      <div className="flex items-center justify-between p-3 text-sm">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">
          Chatwoot account ID
        </span>
        <div className="flex items-center gap-2">
          {current ? (
            <span className="font-mono text-sm font-medium">{current}</span>
          ) : (
            <span className="text-xs text-amber-600 dark:text-amber-400 font-medium">
              Not configured — inbox auto-fill disabled
            </span>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={() => {
              setValue(current ? String(current) : "");
              setEditing(true);
            }}
            data-testid="edit-chatwoot-account-id"
          >
            {current ? "Edit" : "Set"}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2 p-3">
      <Label
        htmlFor="cw-account-id"
        className="text-xs uppercase tracking-wider text-muted-foreground"
      >
        Chatwoot account ID
      </Label>
      <div className="flex items-center gap-2">
        <Input
          id="cw-account-id"
          type="number"
          min={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={isSubmitting}
          placeholder="e.g. 1"
          className="h-8 w-36 text-sm"
          autoFocus
          onKeyDown={(e) => {
            if (e.key === "Enter" && isValid) handleSave();
            if (e.key === "Escape") setEditing(false);
          }}
        />
        <Button
          size="sm"
          className="h-8"
          disabled={!isValid || isSubmitting}
          onClick={handleSave}
        >
          {isSubmitting ? "Saving…" : "Save"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-8"
          disabled={isSubmitting}
          onClick={() => setEditing(false)}
        >
          Cancel
        </Button>
      </div>
      {errorMsg && <p className="text-xs text-destructive">{errorMsg}</p>}
    </div>
  );
}
