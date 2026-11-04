/**
 * Settings screen #15 (Phase 8 PR-4).
 *
 * Server Component. Fetches the current settings envelope server-side
 * and renders <SettingsClient>, which composes the sectioned form +
 * email-change/account-deletion dialogs. Each form section saves
 * independently via PATCH /api/tenant/settings (each section is its
 * own RHF instance — see SettingsForm).
 *
 * Bot persona + the AI agent moved to their own screen (/agent); this
 * screen now covers business hours, handoff, disclosure, and account.
 */

import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";

import { SettingsClient } from "@/components/settings/SettingsClient";
import { FollowupSettingsCard } from "@/components/settings/FollowupSettingsCard";
import { loadSettings } from "@/lib/settings/load";
import { authOptions } from "@/lib/auth/options";

export default async function SettingsPage() {
  const session = await getServerSession(authOptions);
  if (!session?.user?.email) {
    // Defence-in-depth — middleware should redirect unauthenticated
    // hits to /login before this renders, but the page should not
    // silently degrade if the middleware ever shifts (e.g. a future
    // matcher edit drops this route).
    redirect("/login");
  }
  const initial = await loadSettings(session.user.email);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Business hours, handoff messages, disclosure, and your account.
        </p>
      </header>
      <SettingsClient initial={initial} />
      <FollowupSettingsCard />
    </div>
  );
}
