"use client";

/**
 * Settings client wrapper (Phase 8 PR-4).
 *
 * Hosts the SettingsForm + manages the two modal states (email-change
 * stub, account-deletion confirmation). The Settings page itself is
 * a Server Component that fetches data + renders this client wrapper
 * with the initial values.
 */

import * as React from "react";
import Link from "next/link";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  SettingsForm,
  type SettingsInitialValues,
} from "@/components/settings/SettingsForm";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

export function SettingsClient({
  initial,
}: {
  initial: SettingsInitialValues;
}) {
  const [emailOpen, setEmailOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  const [deleteConfirmed, setDeleteConfirmed] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);

  async function submitDeletion() {
    setSubmitting(true);
    try {
      await apiFetch<unknown>(TenantRoutes.privacyDelete(), { method: "POST" });
      toast.success(
        "Account deletion requested. We'll follow up within 24 hours.",
      );
      setDeleteOpen(false);
    } catch (err) {
      const msg =
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't submit your request. Email billing@getpolaris.my.";
      toast.error(msg);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <SettingsForm
        initial={initial}
        onOpenEmailChange={() => setEmailOpen(true)}
        onOpenDeletion={() => {
          setDeleteConfirmed(false);
          setDeleteOpen(true);
        }}
      />

      {/* Email-change stub — full verification round-trip lands in a
          follow-up PR (separate token table + mail-to-new + notice-to-
          old + confirmation endpoint). Until then we route the request
          through billing@. */}
      <Dialog open={emailOpen} onOpenChange={setEmailOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Change sign-in email</DialogTitle>
            <DialogDescription>
              Email changes go through support so we can verify both the current
              and the new address.
            </DialogDescription>
          </DialogHeader>
          <p className="text-sm">
            Email{" "}
            <a
              className="font-medium underline"
              href={`mailto:billing@getpolaris.my?subject=Change%20sign-in%20email&body=Tenant:%20${encodeURIComponent(
                initial.company_name,
              )}%0ACurrent:%20${encodeURIComponent(initial.user_email)}%0ANew:%20`}
            >
              billing@getpolaris.my
            </a>{" "}
            with the new address. We'll confirm with both addresses before
            switching.
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEmailOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Request account deletion</DialogTitle>
            <DialogDescription>
              30-day grace window. We&apos;ll soft-delete first; you can restore
              via support during that period.
            </DialogDescription>
          </DialogHeader>
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
            <li>Bot replies stop at the end of your current paid period.</li>
            <li>
              Conversations, knowledge, and broadcasts move to soft-delete.
            </li>
            <li>
              We hard-purge 30 days later. Audit logs are retained indefinitely
              (hash-chain) for compliance.
            </li>
            <li>
              Need to export your data first?{" "}
              <Link href="/settings/dsar/export" className="underline">
                Run a DSAR export
              </Link>
              .
            </li>
          </ul>
          <label
            htmlFor="acct-deletion-confirm"
            className="flex items-start gap-2 text-sm"
          >
            <input
              id="acct-deletion-confirm"
              type="checkbox"
              checked={deleteConfirmed}
              onChange={(e) => setDeleteConfirmed(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-input"
            />
            <span>
              I understand the consequences and want to request account
              deletion.
            </span>
          </label>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={!deleteConfirmed || submitting}
              onClick={submitDeletion}
            >
              {submitting ? "Submitting…" : "Request deletion"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
