"use client";

/**
 * Portal top bar (Phase 8 PR-3).
 *
 * Shell layout — left: tenant name + plan badge, right: notification
 * bell + help dropdown + user menu. The bell + help dropdown slots are
 * placeholder stubs (PR-11 fills in real polling + Chatwoot
 * help_center links); the user menu is functional now because logout
 * is the load-bearing primary action a logged-in tenant needs.
 */

import * as React from "react";
import { LogOut } from "lucide-react";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { HelpDropdown } from "@/components/shell/HelpDropdown";
import { MobileNav } from "@/components/shell/MobileNav";
import { NotificationBell } from "@/components/shell/NotificationBell";
import { TenantBadge } from "@/components/shell/TenantBadge";
import { useTenantContext } from "@/components/providers/tenant-provider";

function initialsFromEmail(email: string): string {
  const local = email.split("@")[0] ?? "u";
  const parts = local.split(/[._-]/).filter(Boolean);
  if (parts.length >= 2 && parts[0] && parts[1]) {
    return (parts[0][0]! + parts[1][0]!).toUpperCase();
  }
  return local.slice(0, 2).toUpperCase();
}

export function TopBar() {
  const { user_email } = useTenantContext();
  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-card px-4 md:px-6">
      <div className="flex items-center gap-2">
        <MobileNav />
        <TenantBadge />
      </div>
      <div className="flex items-center gap-1">
        {/* Phase 8 PR-11: real notification polling + Chatwoot help_center links. */}
        <NotificationBell />
        <HelpDropdown />

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              className="ml-1 h-9 gap-2 px-2"
              aria-label="User menu"
            >
              <Avatar className="h-7 w-7">
                <AvatarFallback>{initialsFromEmail(user_email)}</AvatarFallback>
              </Avatar>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel className="font-normal">
              <div className="flex flex-col">
                <span className="text-sm font-medium">{user_email}</span>
                <span className="text-xs text-muted-foreground">Signed in</span>
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <a href="/api/auth/signout">
                <LogOut className="h-4 w-4" aria-hidden />
                Sign out
              </a>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
