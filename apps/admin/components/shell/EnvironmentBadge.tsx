/**
 * Environment badge (Phase 9 PR-3).
 *
 * Shows in the admin topbar. Red 'prod' / blue 'staging' / grey 'dev' /
 * indigo 'preview' — the operator can tell at a glance which Polaris
 * environment they're acting on. Per the wireframe brief
 * (_artifacts/admin_dashboard_wireframe_prompt.md §"Global layout").
 *
 * Reads NEXT_PUBLIC_DEPLOY_ENV when present; falls back to NODE_ENV.
 * Server-rendered (read on the server, baked into the layout); a
 * runtime env flip requires a redeploy.
 */

import { cn } from "@/lib/utils";

type EnvLabel = "prod" | "staging" | "preview" | "dev";

function resolveEnv(): EnvLabel {
  // Check build-time NEXT_PUBLIC_DEPLOY_ENV first (baked in at build time,
  // works in both server and client components).
  // Then POLARIS_DEPLOY_ENV — server-only, readable at request time in
  // server components even without a rebuild. Lets the local VPS stack
  // show 'dev' without needing a Docker image rebuild.
  // Accept the dedicated DEPLOY_ENV vars first, then fall back to the
  // POLARIS_ENV / ENVIRONMENT vars the prod compose already sets
  // (POLARIS_ENV=prod, ENVIRONMENT=production) — without that fallback the
  // badge showed 'dev' on production because nobody set POLARIS_DEPLOY_ENV
  // specifically (QA finding L3).
  const raw = (
    process.env.NEXT_PUBLIC_DEPLOY_ENV ??
    process.env.POLARIS_DEPLOY_ENV ??
    process.env.POLARIS_ENV ??
    process.env.ENVIRONMENT ??
    ""
  ).toLowerCase();
  // Normalise 'production' → 'prod' (ENVIRONMENT uses the long form).
  const explicit = raw === "production" ? "prod" : raw;
  if (
    explicit === "prod" ||
    explicit === "staging" ||
    explicit === "preview" ||
    explicit === "dev"
  ) {
    return explicit;
  }
  // Default to 'dev' when nothing is set — local stacks that export none of
  // the above still read as dev (never a false 'prod' from NODE_ENV alone).
  return "dev";
}

const BADGE_STYLES: Record<EnvLabel, string> = {
  prod: "bg-destructive/15 text-destructive border-destructive/30",
  staging: "bg-blue-500/15 text-blue-700 border-blue-500/30 dark:text-blue-300",
  preview:
    "bg-indigo-500/15 text-indigo-700 border-indigo-500/30 dark:text-indigo-300",
  dev: "bg-muted text-muted-foreground border-border",
};

export function EnvironmentBadge() {
  const env = resolveEnv();
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em]",
        BADGE_STYLES[env],
      )}
      aria-label={`environment: ${env}`}
    >
      {env}
    </span>
  );
}
