/**
 * Magic-link sent confirmation (Phase 8 PR-2).
 *
 * NextAuth redirects here after `/api/auth/signin/email` succeeds.
 * It also lands here on rate-limit hit (we mirror the same outcome
 * so a brute-forcer can't distinguish "email accepted" from "email
 * rate-limited").
 *
 * Copy reflects the brand voice from `_artifacts/portal_design_prompt.md`.
 * Expiry minutes derive from MAGIC_LINK_EXPIRY_MIN so a future tweak
 * doesn't require a copy edit.
 */

import { expiryMinutes } from "@/lib/auth/config";

import { Card } from "@/components/ui/card";

export default function VerifyRequestPage() {
  const mins = expiryMinutes();

  return (
    <Card className="p-8">
      <div className="mb-6 flex h-10 w-10 items-center justify-center rounded-full bg-primary/10">
        <svg
          className="h-5 w-5 text-primary"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <rect width="20" height="16" x="2" y="4" rx="2" />
          <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" />
        </svg>
      </div>

      <h1 className="mb-2 text-xl font-semibold tracking-tight text-foreground">
        Check your inbox
      </h1>
      <p className="mb-1 text-sm text-muted-foreground">
        We sent a sign-in link to the email you provided. Click the link to
        finish signing in &mdash; it works once and expires in {mins} minutes.
      </p>
      <p className="text-sm text-muted-foreground">
        Didn&apos;t get it? Check your spam folder, or{" "}
        <a
          href="/login"
          className="font-medium text-primary underline-offset-4 hover:underline"
        >
          try again
        </a>
        .
      </p>
    </Card>
  );
}
