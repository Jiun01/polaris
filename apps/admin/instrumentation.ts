/**
 * Next.js 14 instrumentation hook — Phase 10 PR-2.
 *
 * Next.js calls register() once per process start before the app handles any
 * requests.  We use it to load the correct Sentry config for the runtime.
 *
 * Docs: https://nextjs.org/docs/app/building-your-application/optimizing/instrumentation
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }
  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}
