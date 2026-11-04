"use client";

/**
 * Help dropdown in the topbar (Phase 8 PR-11).
 *
 * Per ADR-035: help_center lives at help.getpolaris.my (Chatwoot
 * help_center subdomain). Calendly + status + support email links
 * complete the row.
 */

import { CircleHelp, ExternalLink } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";

interface HelpLink {
  label: string;
  href: string;
  external?: boolean;
  hint?: string;
}

const HELP_LINKS: HelpLink[] = [
  {
    label: "Knowledge base",
    href: "https://help.getpolaris.my",
    external: true,
    hint: "Guides, walkthroughs, FAQ",
  },
  {
    label: "Email support",
    href: "mailto:support@getpolaris.my",
    hint: "support@getpolaris.my",
  },
  {
    label: "Schedule a call",
    href: "https://calendly.com/polaris-onboarding",
    external: true,
  },
];

export function HelpDropdown() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-9 w-9 p-0"
          aria-label="Help"
        >
          <CircleHelp className="h-4 w-4" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel>Help</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {HELP_LINKS.map((l) => (
          <DropdownMenuItem key={l.href} asChild>
            <a
              href={l.href}
              target={l.external ? "_blank" : undefined}
              rel={l.external ? "noreferrer" : undefined}
              className="flex flex-col items-start gap-0.5"
            >
              <span className="flex items-center gap-1.5">
                {l.label}
                {l.external ? (
                  <ExternalLink className="h-3 w-3" aria-hidden />
                ) : null}
              </span>
              {l.hint ? (
                <span className="text-[11px] text-muted-foreground">
                  {l.hint}
                </span>
              ) : null}
            </a>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
