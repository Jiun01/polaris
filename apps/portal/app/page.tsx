/**
 * Root index (Phase 8 PR-2 — replaces the Phase 0 placeholder).
 *
 * Middleware handles the actual routing: unauthenticated users hit
 * `/login`, authenticated users without `suspended_reason` pass through
 * to here, suspended users land on `/pay`. So by the time this page
 * runs, the user is signed in and not suspended.
 *
 * We redirect to `/dashboard` (PR-3 deliverable). Until PR-3 lands,
 * hitting this redirect in dev will 404 — which is expected because
 * the portal isn't user-deployable yet.
 */

import { redirect } from "next/navigation";

export default function HomePage(): never {
  redirect("/dashboard");
}
