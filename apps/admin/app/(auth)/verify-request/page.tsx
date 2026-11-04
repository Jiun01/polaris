/**
 * Magic-link sent confirmation for admin (Phase 9 PR-2).
 *
 * NextAuth redirects here after /api/auth/signin/email succeeds. Also
 * lands here on rate-limit hit (we mirror the same outcome so an
 * attacker can't distinguish "accepted" from "rate-limited").
 *
 * Copy notes the IP-bind requirement so the user understands why a
 * click from another network is rejected.
 */

import { expiryMinutes } from "@/lib/auth/config";

export default function VerifyRequestPage() {
  const mins = expiryMinutes();

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-8 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <div className="mb-6 flex h-10 w-10 items-center justify-center rounded-full bg-slate-100 dark:bg-slate-800">
        <svg
          className="h-5 w-5 text-slate-700 dark:text-slate-300"
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

      <h1 className="mb-2 text-xl font-semibold text-slate-900 dark:text-slate-50">
        Check your inbox
      </h1>
      <p className="mb-1 text-sm text-slate-600 dark:text-slate-400">
        We sent a sign-in link to the email you provided. Click the link to
        finish signing in &mdash; it works once and expires in {mins} minutes.
      </p>
      <p className="mb-1 text-sm text-slate-600 dark:text-slate-400">
        The link must be opened from the same network you requested it on.
      </p>
      <p className="text-sm text-slate-600 dark:text-slate-400">
        Didn&apos;t get it? Check spam, or{" "}
        <a
          href="/login"
          className="font-medium text-slate-900 underline hover:text-slate-700 dark:text-slate-100 dark:hover:text-slate-300"
        >
          try again
          <span className="sr-only"> — return to the sign-in page</span>
        </a>
        .
      </p>
    </div>
  );
}
