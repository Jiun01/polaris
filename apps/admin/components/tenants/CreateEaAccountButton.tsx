/**
 * CreateEaAccountButton — provisions a tenant's Easy!Appointments account
 * (admin login + default provider + service) and emails the admin a
 * password-reset link.
 *
 * Opens a small dialog so the operator can OPTIONALLY set the EA admin login
 * username; blank → the server derives `admin-<slug>`. The reset-link recipient
 * defaults to the tenant's primary user email; the trigger is disabled when no
 * email is known (nothing to send the link to). On success the toast echoes the
 * final username so it doesn't have to be looked up. A taken/invalid username is
 * surfaced inline and keeps the dialog open.
 */

"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface Props {
  tenantId: string;
  email: string | null;
  slug?: string | null;
}

// Mirrors the API + EA-side validation.
const USERNAME_RE = /^[A-Za-z0-9._-]{3,50}$/;

interface CreateEaAccountResponse {
  provisioned: boolean;
  failure_reason: string | null;
  username: string | null;
}

export function CreateEaAccountButton({ tenantId, email, slug }: Props) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const [loading, setLoading] = useState(false);

  const busy = isPending || loading;
  const placeholder = slug ? `admin-${slug}` : "admin-<slug>";

  async function handleCreate() {
    if (!email) return;

    const trimmed = username.trim();
    if (trimmed && !USERNAME_RE.test(trimmed)) {
      setFieldError("3-50 chars: letters, digits, . _ -");
      return;
    }
    setFieldError(null);
    setLoading(true);
    try {
      const res = await adminFetch<CreateEaAccountResponse>(
        adminRoutes.tenants.createEaAccount(tenantId),
        {
          method: "POST",
          body: trimmed ? { email, username: trimmed } : { email },
        },
      );
      if (res?.provisioned) {
        const login = res.username ? ` Login: ${res.username}.` : "";
        toast.success(
          `EA account created.${login} Reset link sent to ${email}.`,
        );
        setOpen(false);
        setUsername("");
        startTransition(() => {
          router.refresh();
        });
      } else if (res?.failure_reason === "username_taken") {
        setFieldError("That username is already taken — choose another.");
      } else if (res?.failure_reason === "invalid_username") {
        setFieldError("Invalid username (3-50 chars: letters, digits, . _ -).");
      } else {
        toast.warning(
          `EA provisioning did not complete (${res?.failure_reason ?? "unknown"}). Retry once EA is reachable.`,
        );
      }
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to create EA account.";
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) {
          setFieldError(null);
          setUsername("");
        }
      }}
    >
      <DialogTrigger asChild>
        <Button
          type="button"
          variant="default"
          disabled={!email}
          title={
            email
              ? undefined
              : "No tenant email on file to send the reset link to"
          }
        >
          Create EA account
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create EA account</DialogTitle>
          <DialogDescription>
            Provisions the calendar admin + provider and emails a password-reset
            link.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="ea-email">Reset link recipient</Label>
            <Input id="ea-email" value={email ?? ""} readOnly disabled />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ea-username">Username (optional)</Label>
            <Input
              id="ea-username"
              value={username}
              onChange={(e) => {
                setUsername(e.target.value);
                setFieldError(null);
              }}
              placeholder={placeholder}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              Leave blank to auto-generate ({placeholder}).
            </p>
            {fieldError ? (
              <p className="text-xs text-destructive">{fieldError}</p>
            ) : null}
          </div>
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => setOpen(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={handleCreate}
            disabled={busy || !email}
          >
            {busy ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
