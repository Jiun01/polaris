/**
 * Sentry browser-side initialisation — Phase 10 PR-2 (ADR-035).
 *
 * This file is picked up automatically by @sentry/nextjs at build time.
 * It runs in the browser context on every admin page navigation.
 *
 * The admin app runs on an internal-only domain (admin.getpolaris.my) — Sentry
 * still captures JS errors so the operator doesn't chase silent failures.
 */
import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,

  tracesSampleRate: 0.1,

  replaysSessionSampleRate: 0,
  replaysOnErrorSampleRate: 0,

  enabled: !!process.env.NEXT_PUBLIC_SENTRY_DSN,

  environment: process.env.NEXT_PUBLIC_ENVIRONMENT ?? "local",
});
