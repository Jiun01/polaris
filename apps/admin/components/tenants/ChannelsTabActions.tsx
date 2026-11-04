"use client";

/**
 * ChannelsTabActions — Phase 10 PR-0-retrofit.
 *
 * Thin client-component wrapper so the server-rendered ChannelsTab can
 * hand off the AddChannelModal trigger while keeping router.refresh()
 * available (Next.js `useRouter` is client-only).
 */

import { useRouter } from "next/navigation";

import { AddChannelModal } from "@/components/tenants/AddChannelModal";
import type { PlanTier } from "@/components/tenants/types";

interface Props {
  tenantId: string;
  planTier: PlanTier | null;
  isImpersonating: boolean;
}

export function ChannelsTabActions({
  tenantId,
  planTier,
  isImpersonating,
}: Props) {
  const router = useRouter();

  return (
    <AddChannelModal
      tenantId={tenantId}
      planTier={planTier}
      isImpersonating={isImpersonating}
      onSuccess={() => router.refresh()}
    />
  );
}
