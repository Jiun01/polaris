"use client";

/**
 * Login screen — magic-link entry form.
 *
 * Uses next-auth/react signIn("email") so the CSRF token is fetched by the
 * BROWSER (not server-side). A server-side CSRF fetch cannot relay the
 * Set-Cookie header back to the browser, which breaks login for all first-
 * time visitors who have no prior next-auth.csrf-token cookie.
 *
 * signIn() handles CSRF internally, redirects to /verify-request on
 * success, and uses the standard NextAuth error-redirect path on failure.
 */

import { signIn } from "next-auth/react";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function LoginPage() {
  const searchParams = useSearchParams();
  const error = searchParams.get("error");
  // callbackUrl may be relative (/dashboard) or absolute (http://portal.localhost/dashboard).
  // We pass it directly to signIn — NextAuth validates it against NEXTAUTH_URL
  // server-side, so cross-origin redirects are rejected there.
  const callbackUrl = searchParams.get("callbackUrl") ?? "/dashboard";

  const [email, setEmail] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      await signIn("email", { email, callbackUrl });
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <Card className="p-8">
      <CardHeader className="space-y-1.5 p-0">
        <CardTitle className="text-xl">Sign in to your portal</CardTitle>
        <CardDescription>
          Enter the email tied to your Polaris account. We&apos;ll send you a
          sign-in link.
        </CardDescription>
      </CardHeader>

      <CardContent className="p-0 pt-6">
        {error ? (
          <div
            role="alert"
            className="mb-4 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive outline outline-1 outline-destructive/30"
          >
            {error === "AccessDenied"
              ? "That email isn't tied to a Polaris account. If you should have access, contact support."
              : "We couldn't send the sign-in link. Please try again in a few minutes."}
          </div>
        ) : null}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="email">Work email</Label>
            <Input
              id="email"
              name="email"
              type="email"
              required
              autoComplete="email"
              autoFocus
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <Button type="submit" disabled={isSubmitting} className="w-full">
            {isSubmitting ? "Sending…" : "Send sign-in link"}
          </Button>
        </form>

        <p className="mt-6 text-xs text-muted-foreground">
          New to Polaris? Polaris is sales-led at the moment &mdash; contact{" "}
          <a
            href="mailto:hello@getpolaris.my"
            className="text-primary underline-offset-4 hover:underline"
          >
            hello@getpolaris.my
          </a>{" "}
          to get started.
        </p>
      </CardContent>
    </Card>
  );
}
