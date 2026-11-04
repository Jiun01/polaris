/**
 * Impersonation landing page (Phase 9 PR-5).
 *
 * Platform admins navigate here to start an impersonation session for a
 * specific tenant user. The tenant + user are selected from searchParams
 * (tenantId + userId). Without them, shows a usage note.
 *
 * Deep-link from the Tenants detail page: /impersonation?tenantId=...&userId=...&slug=...&email=...
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { PreImpersonationModal } from "@/components/impersonation/PreImpersonationModal";

export default function ImpersonationPage({
  searchParams,
}: {
  searchParams?: {
    tenantId?: string;
    userId?: string;
    slug?: string;
    email?: string;
  };
}) {
  const { tenantId, userId, slug, email } = searchParams ?? {};

  if (!tenantId || !userId) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <h1 className="text-xl font-semibold tracking-tight">Impersonation</h1>
        <p className="text-sm text-muted-foreground">
          To start an impersonation session, navigate here from the Tenant
          detail page using the &quot;Impersonate user&quot; action. A
          platform_admin session and the target tenant user ID are required.
        </p>
        <Link
          href="/tenants"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
          Go to Tenants
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div className="flex items-center gap-2">
        <Link
          href="/tenants"
          className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
          Tenants
        </Link>
      </div>

      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          Impersonate Tenant User
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Start a read-audit session as{" "}
          <span className="font-mono">{email ?? userId}</span> in tenant{" "}
          <span className="font-mono">{slug ?? tenantId}</span>.
        </p>
      </div>

      <div className="rounded-md border border-amber-200 bg-amber-50/50 p-4 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/10 dark:text-amber-300 space-y-1">
        <p className="font-semibold">Security notice</p>
        <ul className="mt-1 list-disc list-inside space-y-0.5 text-xs">
          <li>
            This session does NOT elevate portal privileges. The portal runs as
            the tenant user.
          </li>
          <li>
            Every portal action during the session is audited with your admin
            ID.
          </li>
          <li>
            The session cookie is HttpOnly — it cannot be read by portal
            JavaScript.
          </li>
          <li>The session expires in 60 minutes automatically.</li>
        </ul>
      </div>

      <PreImpersonationModal
        tenantId={tenantId}
        userId={userId}
        tenantSlug={slug ?? tenantId}
        userEmail={email ?? userId}
      />
    </div>
  );
}
