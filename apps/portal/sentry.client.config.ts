/**
 * Sentry browser-side initialisation — Phase 10 PR-2 (ADR-035).
 *
 * This file is picked up automatically by @sentry/nextjs at build time.
 * It runs in the browser context on every page navigation.
 *
 * DSN is public by design (NEXT_PUBLIC_ prefix) — the DSN itself is not a
 * secret; it only allows *sending* events to our project, not reading them.
 * The project is gated by Sentry's own auth after ingestion.
 */
import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,

  // 10 % trace sample at GA — raise if we need detailed client-side tracing.
  tracesSampleRate: 0.1,

  // Session Replay disabled at GA: no user-behaviour recording costs.
  replaysSessionSampleRate: 0,
  replaysOnErrorSampleRate: 0,

  // Only active when the DSN is configured; local dev with no DSN is silent.
  enabled: !!process.env.NEXT_PUBLIC_SENTRY_DSN,

  environment: process.env.NEXT_PUBLIC_ENVIRONMENT ?? "local",
});
