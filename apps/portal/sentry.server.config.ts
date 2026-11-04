/**
 * Sentry Node.js server-side initialisation — Phase 10 PR-2 (ADR-035).
 *
 * Loaded via instrumentation.ts register() hook (Next.js 14 app router).
 * Captures unhandled exceptions and slow server actions.
 */
import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.SENTRY_DSN ?? process.env.NEXT_PUBLIC_SENTRY_DSN,

  tracesSampleRate: 0.1,

  enabled: !!(process.env.SENTRY_DSN ?? process.env.NEXT_PUBLIC_SENTRY_DSN),

  environment:
    process.env.ENVIRONMENT ?? process.env.NEXT_PUBLIC_ENVIRONMENT ?? "local",
});
