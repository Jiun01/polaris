/**
 * Stop impersonation button — client island inside the server ImpersonationBanner.
 */

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { stopImpersonation } from "@/lib/impersonation/start";

export function StopImpersonationButton() {
  const [pending, setPending] = useState(false);
  const router = useRouter();

  async function handleStop() {
    if (pending) return;
    setPending(true);
    try {
      await stopImpersonation();
      router.refresh();
    } catch {
      setPending(false);
    }
  }

  return (
    <button
      onClick={handleStop}
      disabled={pending}
      className="rounded border border-white/40 bg-white/10 px-3 py-0.5 text-xs font-semibold hover:bg-white/20 disabled:opacity-60"
    >
      {pending ? "Stopping…" : "Stop session"}
    </button>
  );
}
