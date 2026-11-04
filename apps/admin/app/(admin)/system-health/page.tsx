import { redirect } from "next/navigation";

/**
 * /system-health — alias for the system health dashboard, which lives at
 * /health. The Phase 20 E2E spec (Act 7) references /system-health by
 * name; this server-component redirect keeps that URL working.
 */
export default function SystemHealthRedirect() {
  redirect("/health");
}
