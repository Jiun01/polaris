/**
 * AI Agent screen.
 *
 * Server Component. Reuses the tenant settings envelope (the same
 * GET /api/tenant/settings the Settings screen reads) and renders the
 * AgentForm: bot persona, the tool-calling agent toggles, and the
 * read-only always-on guardrails. Persona + agent config moved here
 * out of Settings so the bot's behavior lives on its own surface.
 */

import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";

import { AgentForm } from "@/components/agent/AgentForm";
import { loadSettings } from "@/lib/settings/load";
import { authOptions } from "@/lib/auth/options";

export default async function AgentPage() {
  const session = await getServerSession(authOptions);
  if (!session?.user?.email) {
    redirect("/login");
  }
  const initial = await loadSettings(session.user.email);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">AI Agent</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Shape how your bot thinks and replies: its persona, the tools it can
          use, and the guardrails it always follows.
        </p>
      </header>
      <AgentForm initial={initial} />
    </div>
  );
}
