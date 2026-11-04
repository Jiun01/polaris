/**
 * Sentry Edge runtime initialisation — Phase 10 PR-2 (ADR-035).
 *
 * Loaded via instrumentation.ts register() hook for middleware / edge routes.
 */
import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.SENTRY_DSN ?? process.env.NEXT_PUBLIC_SENTRY_DSN,
  tracesSampleRate: 0.1,
  enabled: !!(process.env.SENTRY_DSN ?? process.env.NEXT_PUBLIC_SENTRY_DSN),
});
