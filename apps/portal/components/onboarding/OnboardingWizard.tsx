"use client";

/**
 * First-run onboarding wizard (Phase 8 PR-5).
 *
 * 4-step flow per portal_design_prompt.md "First-run onboarding":
 *   1. Connect WhatsApp     → routes to /channels (sales-led at GA)
 *   2. Upload knowledge     → routes to /knowledge (Phase 8 PR-6)
 *   3. Preview your bot     → opens chat.getpolaris.my agent inbox
 *   4. Done                 → goes to /dashboard
 *
 * Each step has Skip / Continue / Done CTAs. The wizard doesn't lock
 * the user in — they can navigate away via sidebar; the middleware
 * stops redirecting once any channel exists AND any flow is published.
 *
 * Step state is local-only; we don't persist progress server-side at
 * GA. If the tenant_admin closes the browser mid-wizard, the next
 * login lands them back on step 1 (the middleware redirect re-fires).
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Check,
  ChevronRight,
  MessageCircle,
  Sparkles,
  Upload,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface StepDef {
  id: "channel" | "knowledge" | "preview" | "done";
  title: string;
  description: string;
  icon: React.ComponentType<{ className?: string }>;
  primaryLabel: string;
  primaryHref?: string;
}

const STEPS: ReadonlyArray<StepDef> = [
  {
    id: "channel",
    title: "Connect WhatsApp",
    description:
      "Email billing@getpolaris.my with your WhatsApp Business token, phone number ID, and WABA ID. We'll wire up your messaging inbox within one business day.",
    icon: MessageCircle,
    primaryLabel: "Email support to connect",
  },
  {
    id: "knowledge",
    title: "Upload your knowledge",
    description:
      "Your bot grounds replies on documents you upload, like pricing sheets, FAQs, and opening hours. Add at least one document so the first replies are accurate.",
    icon: Upload,
    primaryLabel: "Add knowledge",
    primaryHref: "/knowledge",
  },
  {
    id: "preview",
    title: "Preview your bot",
    description:
      "Open your messaging inbox and message your own WhatsApp number from a personal phone. The bot replies in seconds, so you can confirm tone, accuracy, and handoff feel right.",
    icon: Sparkles,
    primaryLabel: "Open inbox",
    primaryHref: "https://chat.getpolaris.my",
  },
  {
    id: "done",
    title: "You're set",
    description:
      "Customers can now message your WhatsApp and the bot will answer. Tweak the persona and business hours from Settings whenever you want.",
    icon: Check,
    primaryLabel: "Go to Dashboard",
    primaryHref: "/dashboard",
  },
];

export function OnboardingWizard() {
  const router = useRouter();
  const [index, setIndex] = React.useState(0);

  const step = STEPS[index]!;
  const isLast = index === STEPS.length - 1;
  const Icon = step.icon;

  function next() {
    if (isLast) {
      // Mark onboarding complete so the dashboard's shouldRedirectToOnboarding
      // check doesn't loop back here when the tenant hasn't connected a channel
      // yet (completing all steps is intent to proceed, same as skipping).
      if (typeof document !== "undefined") {
        document.cookie =
          "polaris_onboarding_skipped=1; path=/; max-age=2592000; samesite=lax";
      }
      router.push("/dashboard");
    } else {
      setIndex((i) => Math.min(i + 1, STEPS.length - 1));
    }
  }

  function back() {
    setIndex((i) => Math.max(i - 1, 0));
  }

  function skipAll() {
    // Set a 30-day cookie so the dashboard's fresh-tenant redirect
    // doesn't fire repeatedly after explicit skip. Path=/ so it's
    // visible to the dashboard's server-side cookies() read. The
    // tenant can still navigate to /onboarding manually whenever
    // they want; clearing the cookie via re-completion of the wizard
    // is unnecessary because the redirect also stops once any
    // channel is connected.
    if (typeof document !== "undefined") {
      document.cookie =
        "polaris_onboarding_skipped=1; path=/; max-age=2592000; samesite=lax";
    }
    router.push("/dashboard");
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {/* Progress bar */}
      <ol className="flex items-center gap-2" aria-label="Onboarding progress">
        {STEPS.map((s, i) => {
          const reached = i <= index;
          return (
            <li
              key={s.id}
              className={cn(
                "h-1 flex-1 rounded-full transition-colors",
                reached ? "bg-primary" : "bg-muted",
              )}
              aria-current={i === index ? "step" : undefined}
            />
          );
        })}
      </ol>
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        Step {index + 1} of {STEPS.length}
      </p>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <div
              className="flex h-10 w-10 items-center justify-center rounded-md bg-muted text-foreground"
              aria-hidden
            >
              <Icon className="h-5 w-5" />
            </div>
            <div>
              <CardTitle className="text-xl">{step.title}</CardTitle>
              <CardDescription>{step.description}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap gap-2">
            {step.id === "channel" ? (
              <Button asChild>
                <a href="mailto:billing@getpolaris.my?subject=Connect%20WhatsApp">
                  {step.primaryLabel}
                </a>
              </Button>
            ) : step.primaryHref ? (
              step.primaryHref.startsWith("http") ? (
                <Button asChild>
                  <a href={step.primaryHref} target="_blank" rel="noreferrer">
                    {step.primaryLabel}
                  </a>
                </Button>
              ) : (
                <Button asChild>
                  <Link href={step.primaryHref}>{step.primaryLabel}</Link>
                </Button>
              )
            ) : null}
            {!isLast ? (
              <Button variant="ghost" onClick={next}>
                Skip for now
                <ChevronRight className="ml-1 h-4 w-4" aria-hidden />
              </Button>
            ) : null}
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {index > 0 ? (
              <Button variant="ghost" size="sm" onClick={back}>
                Back
              </Button>
            ) : null}
            <Button variant="default" size="sm" onClick={next}>
              {isLast ? "Finish" : "Continue"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          Need help?{" "}
          <a className="underline" href="mailto:billing@getpolaris.my">
            Email support
          </a>
        </span>
        <button
          type="button"
          className="underline hover:text-foreground"
          onClick={skipAll}
        >
          Skip onboarding
        </button>
      </div>
    </div>
  );
}
