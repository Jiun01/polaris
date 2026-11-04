/**
 * Transient "service unavailable" screen (Phase 10 — ban resilience).
 *
 * Rendered by the (portal) layout when the tenant-context lookup fails with a
 * `DbUnavailableError` (DB refused / timed out / circuit open — e.g. a Supabase
 * network ban). We deliberately DON'T `redirect("/login")` in that case: /login
 * hits the same DB, so the user would bounce in a loop and see a raw 500. This
 * card is a calm, branded dead-end that self-heals on retry once the DB is back.
 *
 * Server Component, no client JS — the "Try again" link is a plain re-navigation
 * so it works even if hydration never runs. Markup mirrors the (auth) cards
 * (verify-request / login) for visual consistency.
 */

export function ServiceUnavailable() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-background px-4 py-12">
      <div className="w-full max-w-md">
        <div className="rounded-xl bg-card p-8 outline outline-1 -outline-offset-1 outline-border">
          <div className="mb-6 flex h-10 w-10 items-center justify-center rounded-full bg-n-amber-3">
            <svg
              className="h-5 w-5 text-n-amber-11"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
              <path d="M12 9v4" />
              <path d="M12 17h.01" />
            </svg>
          </div>

          <h1 className="mb-2 text-xl font-semibold tracking-tight text-foreground">
            We&apos;re having trouble connecting
          </h1>
          <p className="mb-6 text-sm text-muted-foreground">
            The portal can&apos;t reach the database right now. This is usually
            temporary &mdash; please try again in a moment. If it persists,
            contact{" "}
            <a
              href="mailto:hello@getpolaris.my"
              className="font-medium text-primary underline-offset-4 hover:underline"
            >
              hello@getpolaris.my
            </a>
            .
          </p>

          <a
            href="/dashboard"
            className="inline-flex w-full items-center justify-center rounded-lg bg-primary px-4 py-2 text-sm font-[460] text-primary-foreground transition-all duration-100 ease-out hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            Try again
          </a>
        </div>
      </div>
    </main>
  );
}
