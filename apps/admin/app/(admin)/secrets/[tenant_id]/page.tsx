/**
 * Secrets rotation surface (Phase 9 PR-4).
 *
 * Server component. Shows per-service rotation forms (Meta WhatsApp,
 * Instagram, Messenger, Chatwoot) and rotation history from audit_logs.
 *
 * Each service renders a RotationForm (client component) with a
 * password-type input that is cleared after a successful rotation.
 * The server side fetches rotation history (audit action=secret.rotated)
 * for this tenant and passes it to RotationHistory.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { formatMyt } from "@/lib/format";
import { RotationForm } from "@/components/secrets/RotationForm";
import {
  RotationHistory,
  type RotationEntry,
} from "@/components/secrets/RotationHistory";
import {
  EmailConfigSection,
  type EmailInboxConfig,
} from "@/components/secrets/EmailConfigSection";

interface TenantStub {
  id: string;
  display_name: string;
  slug: string;
}

interface AuditLogRow {
  id: string;
  created_at: string;
  action: string;
  actor: string | null;
  after: Record<string, unknown> | null;
}

interface TenantDetailResponse {
  id: string;
  display_name: string;
  slug: string;
  recent_audit: Array<{
    id: string;
    occurred_at: string;
    action: string;
    actor_label: string;
    after: Record<string, unknown> | null;
  }>;
}

const SERVICES = [
  "meta_whatsapp",
  "meta_ig",
  "meta_messenger",
  "chatwoot",
  "telegram_bot_token",
] as const;

type ServiceKind = (typeof SERVICES)[number];

const SERVICE_LABELS: Record<ServiceKind, string> = {
  meta_whatsapp: "Meta WhatsApp",
  meta_ig: "Meta Instagram",
  meta_messenger: "Meta Messenger",
  chatwoot: "Chatwoot",
  telegram_bot_token: "Telegram Bot Token",
};

async function fetchTenant(
  tenantId: string,
): Promise<TenantDetailResponse | null> {
  try {
    const res = await adminServerFetch<TenantDetailResponse>(
      adminRoutes.tenants.detail(tenantId),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

interface EmailConfigResponse {
  inboxes: EmailInboxConfig[];
  error: string | null;
}

async function fetchEmailConfig(
  tenantId: string,
): Promise<EmailConfigResponse> {
  try {
    const res = await adminServerFetch<EmailConfigResponse>(
      adminRoutes.tenants.emailConfig(tenantId),
    );
    return res ?? { inboxes: [], error: null };
  } catch (err) {
    const msg =
      err instanceof ApiError ? `API error ${err.status}` : "fetch failed";
    return { inboxes: [], error: msg };
  }
}

function extractRotationHistory(tenant: TenantDetailResponse): RotationEntry[] {
  return tenant.recent_audit
    .filter((row) => row.action === "secret.rotated")
    .map((row) => ({
      id: row.id,
      occurred_at: row.occurred_at,
      service: (row.after?.service as string | undefined) ?? "unknown",
      actor_label: row.actor_label,
    }));
}

export default async function SecretsRotationPage({
  params,
}: {
  params: { tenant_id: string };
}) {
  const [tenant, emailConfig] = await Promise.all([
    fetchTenant(params.tenant_id),
    fetchEmailConfig(params.tenant_id),
  ]);
  if (!tenant) notFound();

  const rotationHistory = extractRotationHistory(tenant);

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Link
        href={`/tenants/${tenant.id}`}
        className="text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        Back to tenant detail
      </Link>

      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          Secrets: {tenant.display_name}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Rotate per-service credentials. The new value is encrypted with PyNaCl
          AEAD and stored in tenant_secrets. It is never echoed back.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {SERVICES.map((service) => (
          <RotationForm key={service} tenantId={tenant.id} service={service} />
        ))}
      </div>

      <EmailConfigSection
        tenantId={tenant.id}
        inboxes={emailConfig.inboxes}
        error={emailConfig.error}
      />

      <RotationHistory entries={rotationHistory} />
    </div>
  );
}
