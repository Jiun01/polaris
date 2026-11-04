/**
 * Pre-impersonation confirmation modal (Phase 9 PR-5).
 *
 * Shown before the admin starts an impersonation session.
 * Confirms the operator's intent and explains what will happen:
 *   - A signed HttpOnly cookie is set on the portal domain.
 *   - Every portal action while the session is active is audited
 *     with actor=admin_id + impersonating_id=tenant_user_id.
 *   - The session expires in 60 minutes.
 *   - NO privilege escalation — the portal tab runs as the tenant user.
 *
 * On confirm: calls startImpersonation and opens portal in new tab.
 */

"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  startImpersonation,
  stopImpersonation,
} from "@/lib/impersonation/start";

interface Props {
  tenantId: string;
  userId: string;
  tenantSlug: string;
  userEmail: string;
  portalBaseUrl?: string;
}

type State = "idle" | "confirming" | "starting" | "done" | "error";

export function PreImpersonationModal({
  tenantId,
  userId,
  tenantSlug,
  userEmail,
  portalBaseUrl = process.env.NEXT_PUBLIC_PORTAL_BASE_URL ??
    "https://portal.getpolaris.my",
}: Props) {
  const [state, setState] = useState<State>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const confirmRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (state === "confirming") {
      confirmRef.current?.focus();
    } else if (state === "idle") {
      triggerRef.current?.focus();
    }
  }, [state]);

  async function handleConfirm() {
    setState("starting");
    setErrorMsg(null);
    try {
      await startImpersonation(tenantId, userId);
      setState("done");
      // Open portal in new tab — the HttpOnly cookie rides in the browser.
      window.open(portalBaseUrl, "_blank", "noopener,noreferrer");
    } catch (err) {
      setState("error");
      setErrorMsg(err instanceof Error ? err.message : "Unknown error");
    }
  }

  if (state === "idle") {
    return (
      <Button
        ref={triggerRef}
        variant="outline"
        size="sm"
        onClick={() => setState("confirming")}
        className="gap-1.5 text-amber-700 border-amber-300 hover:bg-amber-50 dark:text-amber-400 dark:border-amber-700 dark:hover:bg-amber-950/20"
      >
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
        Impersonate
      </Button>
    );
  }

  if (state === "confirming") {
    return (
      <div
        ref={confirmRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="impersonation-confirm-heading"
        tabIndex={-1}
        onKeyDown={(e) => {
          if (e.key === "Escape") setState("idle");
        }}
        className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-700 dark:bg-amber-950/20"
      >
        <h2
          id="impersonation-confirm-heading"
          className="text-sm font-semibold text-amber-900 dark:text-amber-200"
        >
          Start impersonation session?
        </h2>
        <ul className="mt-2 space-y-1 text-amber-800 dark:text-amber-300 text-xs">
          <li>
            You will open the portal as <strong>{userEmail}</strong> (tenant:{" "}
            {tenantSlug}).
          </li>
          <li>
            Every portal action is audited with your admin ID as the actor.
          </li>
          <li>
            The session cookie expires in 60 minutes and does NOT escalate
            privileges.
          </li>
          <li>A banner will appear in the portal tab for the duration.</li>
        </ul>
        <div className="mt-3 flex gap-2">
          <Button size="sm" variant="destructive" onClick={handleConfirm}>
            Start session
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setState("idle")}>
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  if (state === "starting") {
    return (
      <div role="status" className="text-xs text-muted-foreground">
        Starting session…
      </div>
    );
  }

  if (state === "done") {
    async function handleStop() {
      try {
        await stopImpersonation();
      } finally {
        setState("idle");
      }
    }
    return (
      <div
        data-testid="impersonation-banner"
        className="flex items-center gap-3 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs dark:border-emerald-700 dark:bg-emerald-950/20"
      >
        <span role="status" className="text-emerald-700 dark:text-emerald-400">
          Session started. Portal opened in new tab.
        </span>
        <Button
          size="sm"
          variant="outline"
          onClick={handleStop}
          className="h-6 px-2 text-xs"
        >
          Stop session
        </Button>
      </div>
    );
  }

  // error
  return (
    <div
      role="alert"
      className="rounded-md border border-red-300 bg-red-50 p-3 text-xs text-red-700 dark:border-red-700 dark:bg-red-950/20 dark:text-red-400"
    >
      Failed to start session: {errorMsg}
      <button className="ml-2 underline" onClick={() => setState("idle")}>
        Retry
      </button>
    </div>
  );
}
