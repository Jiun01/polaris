"use client";

/**
 * Channel tile (Phase 8 PR-5).
 *
 * Renders one row per channel kind on the Channels page. Three states:
 *   - Connected (>=1 inbox)          → green dot + inbox count
 *   - Not connected (Pro+ tier)      → read-only status note (channels
 *                                       connect automatically once set up)
 *   - Not connected, tier-locked     → upgrade card variant (Starter sees
 *                                       this on IG + Messenger per ADR-015)
 *
 * Read-only status view: no self-serve connect CTA here.
 */

import * as React from "react";
import Link from "next/link";
import { type LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { type ChannelKind } from "@/lib/channel-kinds";

export interface ChannelTileProps {
  // Legacy "instagram" included: tenant_channels rows provisioned before
  // the Instagram Login migration can still carry the legacy FB-page
  // kind; the tile must render old rows without crashing. See plan
  // Decision #2 ("instagram" is a superset-for-display concern on the
  // tenant_channels surface — distinct from the conversations surface,
  // where migration 109 dropped it entirely).
  kind: ChannelKind | "instagram";
  label: string;
  icon: LucideIcon;
  inboxCount: number;
  tierLocked: boolean;
}

export function ChannelTile({
  kind,
  label,
  icon: Icon,
  inboxCount,
  tierLocked,
}: ChannelTileProps) {
  const connected = inboxCount > 0;

  return (
    <Card
      className={cn(
        "flex flex-col",
        tierLocked && !connected && "border-dashed bg-muted/30",
      )}
    >
      <CardContent className="flex flex-1 flex-col gap-3 p-5">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <div
              className={cn(
                "flex h-10 w-10 items-center justify-center rounded-md",
                connected
                  ? "bg-success/10 text-success"
                  : "bg-muted text-muted-foreground",
              )}
              aria-hidden
            >
              <Icon className="h-5 w-5" />
            </div>
            <div>
              <p className="text-sm font-semibold">{label}</p>
              <p className="text-xs text-muted-foreground">
                {connected
                  ? `${inboxCount} ${inboxCount === 1 ? "inbox" : "inboxes"} connected`
                  : tierLocked
                    ? "Available on Pro and above"
                    : "Not connected"}
              </p>
            </div>
          </div>
          {connected ? (
            <Badge variant="success">Active</Badge>
          ) : tierLocked ? (
            <Badge variant="outline">Pro+</Badge>
          ) : null}
        </div>

        <div className="mt-auto pt-2">
          {connected ? (
            <p className="text-xs text-muted-foreground">
              Manage agents and conversations in{" "}
              <a
                className="underline"
                href="https://chat.getpolaris.my"
                target="_blank"
                rel="noreferrer"
              >
                your messaging inbox
              </a>
              .
            </p>
          ) : tierLocked ? (
            <Button asChild variant="default" size="sm" className="w-full">
              <Link href="/billing/plan-change">Upgrade to Pro</Link>
            </Button>
          ) : (
            <p className="text-xs text-muted-foreground">
              Connects automatically once set up.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
