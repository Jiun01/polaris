/**
 * Tenant detail (Phase 9 PR-3, Deliverable #1).
 *
 * Tab-based detail page. Tabs are URL-driven (?tab=overview|channels|
 * templates|branches|lifecycle) so deep links and back-button history
 * work without a client store.
 *
 * Tabs at PR-3:
 *   - Overview      — identity + tier + status + suspended_reason
 *   - Channels      — read-only list of tenant_channels for this tenant
 *   - Templates     — placeholder (full surface in later PR; admin only
 *                     observes here)
 *   - Branches      — placeholder
 *   - Lifecycle     — suspended history + DSAR-delete CTA
 *
 * Per-tab content is rendered server-side. The "Suspend" / "Unsuspend"
 * action buttons are wrapped in form-actions that POST to the API and
 * revalidate the route.
 */

import Link from "next/link";
import { cookies } from "next/headers";
import { notFound } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { formatMyr, formatMyt } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ChannelsTabActions } from "@/components/tenants/ChannelsTabActions";
import { RemoveChannelButton } from "@/components/tenants/RemoveChannelButton";
import { ChatwootAccountField } from "@/components/tenants/ChatwootAccountField";
import { SuspendButton } from "@/components/tenants/SuspendButton";
import { SoftDeleteButton } from "@/components/tenants/SoftDeleteButton";
import { HardDeleteButton } from "@/components/tenants/HardDeleteButton";
import { CreateEaAccountButton } from "@/components/tenants/CreateEaAccountButton";
import { DeleteEaAccountButton } from "@/components/tenants/DeleteEaAccountButton";
import type { TenantDetail, TenantStatus } from "@/components/tenants/types";

const TABS = [
  "overview",
  "channels",
  "templates",
  "branches",
  "lifecycle",
] as const;
type Tab = (typeof TABS)[number];

function parseTab(raw: string | undefined): Tab {
  if (!raw) return "overview";
  return (TABS as readonly string[]).includes(raw) ? (raw as Tab) : "overview";
}

const STATUS_BADGE: Record<TenantStatus, string> = {
  active: "bg-success/15 text-success border-success/30",
  suspended: "bg-warning/15 text-warning border-warning/30",
  deleted: "bg-muted text-muted-foreground border-border",
};

async function fetchTenant(tenantId: string): Promise<TenantDetail | null> {
  try {
    const res = await adminServerFetch<TenantDetail>(
      adminRoutes.tenants.detail(tenantId),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      return null;
    }
    throw err;
  }
}

export default async function TenantDetailPage({
  params,
  searchParams,
}: {
  params: { tenant_id: string };
  searchParams?: { tab?: string };
}) {
  const tenant = await fetchTenant(params.tenant_id);
  if (!tenant) notFound();

  const tab = parseTab(searchParams?.tab);

  // Security invariant: if an impersonation session cookie is present the
  // admin is acting as a tenant user — the channel override UI must be hidden.
  const cookieStore = cookies();
  const isImpersonating = !!cookieStore.get("pol_impersonation")?.value;

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <header className="flex flex-col gap-2">
        <Link
          href="/tenants"
          className="text-xs text-muted-foreground hover:text-foreground hover:underline"
        >
          ← Back to tenants
        </Link>
        <div className="flex items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              {tenant.display_name}
            </h1>
            <p className="text-sm text-muted-foreground">
              <code className="rounded bg-muted px-1.5 py-0.5">
                {tenant.slug}
              </code>
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Badge
              variant="outline"
              className={cn(
                "font-medium capitalize",
                STATUS_BADGE[tenant.status],
              )}
            >
              {tenant.status === "suspended" && tenant.suspended_reason
                ? `${tenant.status}: ${tenant.suspended_reason.replace("_", " ")}`
                : tenant.status}
            </Badge>
            <Badge variant="outline" className="capitalize">
              {tenant.plan_tier ?? "—"}
            </Badge>
            <Button asChild variant="outline" size="sm">
              <Link href={`/secrets/${tenant.id}`}>Manage secrets →</Link>
            </Button>
          </div>
        </div>
      </header>

      <nav className="flex items-center gap-1.5 border-b border-border">
        {TABS.map((t) => {
          const active = tab === t;
          return (
            <Link
              key={t}
              href={`/tenants/${tenant.id}?tab=${t}`}
              className={cn(
                "border-b-2 px-3 py-2 text-sm font-medium capitalize transition",
                active
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
              aria-current={active ? "page" : undefined}
            >
              {t}
            </Link>
          );
        })}
      </nav>

      {tab === "overview" && <OverviewTab tenant={tenant} />}
      {tab === "channels" && (
        <ChannelsTab tenant={tenant} isImpersonating={isImpersonating} />
      )}
      {tab === "templates" && <PlaceholderTab label="Templates" />}
      {tab === "branches" && <PlaceholderTab label="Branches" />}
      {tab === "lifecycle" && <LifecycleTab tenant={tenant} />}
    </div>
  );
}

function OverviewTab({ tenant }: { tenant: TenantDetail }) {
  // Impersonation deep-link: needs a concrete target user. Hidden for deleted
  // tenants and when the tenant has no provisioned user yet.
  const canImpersonate =
    tenant.status !== "deleted" && !!tenant.primary_user_id;
  const impersonateHref = canImpersonate
    ? `/impersonation?tenantId=${encodeURIComponent(tenant.id)}` +
      `&userId=${encodeURIComponent(tenant.primary_user_id as string)}` +
      `&slug=${encodeURIComponent(tenant.slug)}` +
      (tenant.primary_user_email
        ? `&email=${encodeURIComponent(tenant.primary_user_email)}`
        : "")
    : null;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-foreground">Overview</h2>
        {impersonateHref ? (
          <Button asChild variant="outline" size="sm">
            <Link href={impersonateHref}>Impersonate user</Link>
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            disabled
            title={
              tenant.status === "deleted"
                ? "Tenant is deleted"
                : "No provisioned user to impersonate yet"
            }
          >
            Impersonate user
          </Button>
        )}
      </div>
      <Card className="divide-y divide-border">
        <Row label="Display name" value={tenant.display_name} />
        <Row label="Slug" value={tenant.slug} />
        <Row label="Business name" value={tenant.business_name ?? "—"} />
        <Row label="SSM" value={tenant.ssm ?? "—"} />
        <Row label="Vertical" value={tenant.vertical ?? "—"} />
        <Row label="Timezone" value={tenant.timezone ?? "—"} />
        <ChatwootAccountField
          tenantId={tenant.id}
          current={tenant.chatwoot_account_id}
        />
        <Row label="Contact email" value={tenant.contact_email ?? "—"} />
        <Row label="Contact phone" value={tenant.contact_phone ?? "—"} />
        <Separator />
        <Row label="Plan tier" value={tenant.plan_tier ?? "—"} />
        <Row
          label="MRR"
          value={tenant.mrr_myr === null ? "—" : formatMyr(tenant.mrr_myr)}
        />
        <Row label="Users" value={String(tenant.user_count)} />
        <Row label="Channels connected" value={String(tenant.channel_count)} />
        <Row label="Last inbound" value={formatMyt(tenant.last_inbound_at)} />
        <Row label="Created" value={formatMyt(tenant.created_at)} />
      </Card>
    </div>
  );
}

function ChannelsTab({
  tenant,
  isImpersonating,
}: {
  tenant: TenantDetail;
  isImpersonating: boolean;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-foreground">
          Connected channels
        </h2>
        <ChannelsTabActions
          tenantId={tenant.id}
          planTier={tenant.plan_tier}
          isImpersonating={isImpersonating}
        />
      </div>

      {tenant.channels.length === 0 ? (
        <Card className="border-dashed p-6 text-sm text-muted-foreground">
          No channels connected for this tenant yet.
        </Card>
      ) : (
        <Card className="divide-y divide-border">
          {tenant.channels.map((ch) => {
            // Per-channel connection identifiers — only the ones actually set
            // for this channel kind are shown (a WhatsApp channel has no FB
            // Page ID, etc.). The access token is rendered separately below so
            // it always appears (as the masked last-4 or "not set").
            const fields: Array<[string, string]> = (
              [
                [
                  "CW Inbox ID",
                  ch.chatwoot_inbox_id != null
                    ? String(ch.chatwoot_inbox_id)
                    : null,
                ],
                ["WABA ID", ch.waba_id],
                ["Phone Number ID", ch.phone_number_id],
                ["FB Page ID", ch.fb_page_id],
                ["IG Business Account ID", ch.ig_business_account_id],
                ["WA Messaging Tier", ch.wa_messaging_tier],
                ["MM-Lite Status", ch.mm_lite_status],
                [
                  "Telegram Bot",
                  ch.telegram_bot_username
                    ? `@${ch.telegram_bot_username}`
                    : null,
                ],
              ] as Array<[string, string | null]>
            ).filter((f): f is [string, string] => f[1] != null);

            return (
              <div key={ch.id} className="space-y-2 p-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium capitalize">{ch.kind}</p>
                  <div className="flex items-center gap-3">
                    <Badge
                      variant="outline"
                      className={
                        ch.connected
                          ? "border-success/30 bg-success/15 text-success"
                          : "border-border bg-muted text-muted-foreground"
                      }
                    >
                      {ch.connected ? "connected" : "disconnected"}
                    </Badge>
                    <RemoveChannelButton
                      tenantId={tenant.id}
                      channelKind={ch.kind}
                      channelLabel={
                        ch.kind.charAt(0).toUpperCase() + ch.kind.slice(1)
                      }
                      isImpersonating={isImpersonating}
                    />
                  </div>
                </div>

                <div className="space-y-1 text-xs">
                  {fields.map(([label, value]) => (
                    <div key={label} className="flex gap-2">
                      <span className="min-w-[150px] shrink-0 text-muted-foreground">
                        {label}
                      </span>
                      <span className="break-all font-mono text-foreground">
                        {value}
                      </span>
                    </div>
                  ))}
                  <div className="flex gap-2">
                    <span className="min-w-[150px] shrink-0 text-muted-foreground">
                      Access Token
                    </span>
                    {ch.access_token_last4 ? (
                      <span className="font-mono text-foreground">
                        {ch.access_token_last4}
                      </span>
                    ) : (
                      <span className="italic text-muted-foreground">
                        not set
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </Card>
      )}
    </div>
  );
}

function PlaceholderTab({ label }: { label: string }) {
  return (
    <Card className="border-dashed p-6 text-sm text-muted-foreground">
      {label} — admin observes only; tenant CRUD lives in the portal at GA.
    </Card>
  );
}

function LifecycleTab({ tenant }: { tenant: TenantDetail }) {
  return (
    <div className="space-y-4">
      <Card className="space-y-3 p-4">
        <h2 className="text-sm font-semibold">Suspension</h2>
        <p className="text-sm text-muted-foreground">
          {tenant.status === "suspended"
            ? `Currently suspended (${tenant.suspended_reason ?? "manual"}). Unsuspending restores tenant operations immediately.`
            : "Tenant is active. Suspending stops bot replies and may lock the portal per ADR-049."}
        </p>
        <SuspendButton
          tenantId={tenant.id}
          suspended={tenant.status === "suspended"}
        />
      </Card>

      <Card className="space-y-3 p-4">
        <h2 className="text-sm font-semibold">Easy!Appointments account</h2>
        <p className="text-sm text-muted-foreground">
          {tenant.ea_account?.status === "active"
            ? "EA account provisioned. The tenant manages bookings at calendar.getpolaris.my."
            : "No EA account yet. Create one to seed a default provider/service and email the tenant a password-reset link."}
        </p>
        {tenant.ea_account?.status === "active" ? (
          <DeleteEaAccountButton
            tenantId={tenant.id}
            tenantSlug={tenant.slug}
          />
        ) : (
          <CreateEaAccountButton
            tenantId={tenant.id}
            email={tenant.primary_user_email ?? tenant.contact_email}
            slug={tenant.slug}
          />
        )}
      </Card>

      <Card className="space-y-3 border-destructive/30 bg-destructive/5 p-4">
        <h2 className="text-sm font-semibold text-destructive">Soft Delete</h2>
        <p className="text-sm text-destructive/80">
          Marks the tenant as deleted, stops the bot, and locks portal access. A
          30-day grace window runs before permanent purge (ADR-029). Data is
          preserved during grace — this is reversible by a DB operator.
        </p>
        {tenant.status === "deleted" ? (
          <p className="text-xs text-muted-foreground">
            Already soft-deleted on{" "}
            {tenant.deleted_at ? formatMyt(tenant.deleted_at) : "—"}.
          </p>
        ) : (
          <SoftDeleteButton tenantId={tenant.id} tenantSlug={tenant.slug} />
        )}
      </Card>

      <Card className="space-y-3 border-destructive/50 bg-destructive/10 p-4">
        <h2 className="text-sm font-semibold text-destructive">
          Permanent Deletion
        </h2>
        <p className="text-sm text-destructive/80">
          Irreversible physical purge. Destroys all rows — conversations,
          secrets, audit logs, invoices — with no recovery path. The tenant must
          be soft-deleted first.
        </p>
        <HardDeleteButton
          tenantId={tenant.id}
          tenantSlug={tenant.slug}
          tenantStatus={tenant.status}
        />
      </Card>

      {tenant.recent_audit.length > 0 && (
        <Card className="p-4">
          <h2 className="mb-3 text-sm font-semibold">Recent audit (last 10)</h2>
          <ul className="space-y-2 text-sm">
            {tenant.recent_audit.map((row) => (
              <li
                key={row.id}
                className="flex items-baseline justify-between gap-3 border-b border-border pb-2 last:border-b-0 last:pb-0"
              >
                <div>
                  <p className="font-medium">{row.action}</p>
                  <p className="text-xs text-muted-foreground">
                    by {row.actor_label}
                  </p>
                </div>
                <span className="text-xs text-muted-foreground">
                  {formatMyt(row.occurred_at)}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 p-3 text-sm">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <span className="font-medium text-foreground">{value}</span>
    </div>
  );
}
