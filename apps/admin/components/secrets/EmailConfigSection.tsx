"use client";

/**
 * EmailConfigSection — read-only IMAP/SMTP config display + password rotation
 * panels for email inboxes.
 *
 * Email channels are Chatwoot-native: there is no tenant_channels row in
 * Polaris for email. The backend lists the tenant's Chatwoot inboxes and
 * filters for Channel::Email. Passwords are never returned by Chatwoot, so
 * this component shows all non-secret config fields and lets admins push a
 * new IMAP/SMTP password directly to Chatwoot.
 */

import { useEffect, useRef, useState } from "react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";

export interface EmailInboxConfig {
  inbox_id: number;
  inbox_name: string | null;
  email: string | null;
  imap_enabled: boolean;
  imap_address: string | null;
  imap_port: number | null;
  imap_login: string | null;
  imap_enable_ssl: boolean;
  imap_authentication: string | null;
  smtp_enabled: boolean;
  smtp_address: string | null;
  smtp_port: number | null;
  smtp_domain: string | null;
  smtp_login: string | null;
  smtp_enable_ssl_tls: boolean;
  smtp_enable_starttls_auto: boolean;
  smtp_openssl_verify_mode: string | null;
  smtp_authentication: string | null;
}

interface PasswordUpdatePanelProps {
  tenantId: string;
  inboxId: number;
  field: "imap_password" | "smtp_password";
  label: string;
}

function PasswordUpdatePanel({
  tenantId,
  inboxId,
  field,
  label,
}: PasswordUpdatePanelProps) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [status, setStatus] = useState<
    "idle" | "loading" | "success" | "error"
  >("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const resetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clear the post-success reset timer on unmount so state updates never fire
  // on a detached component (e.g. user navigates away during the 2s window).
  useEffect(() => {
    return () => {
      if (resetTimerRef.current !== null) {
        clearTimeout(resetTimerRef.current);
      }
    };
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!value.trim()) return;
    setStatus("loading");
    setErrorMsg(null);
    try {
      await adminFetch(
        adminRoutes.tenants.emailCredentials(tenantId, inboxId),
        {
          method: "PATCH",
          body: { field, new_value: value },
        },
      );
      setStatus("success");
      setValue("");
      resetTimerRef.current = setTimeout(() => {
        setStatus("idle");
        setOpen(false);
      }, 2000);
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "Update failed");
    }
  }

  if (!open) {
    return (
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
        data-testid={`update-${field}-${inboxId}`}
      >
        Update {label}
      </Button>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mt-2 flex items-end gap-2"
      data-testid={`update-form-${field}-${inboxId}`}
    >
      <div className="flex-1">
        <Label htmlFor={`${field}-${inboxId}`} className="sr-only">
          {label}
        </Label>
        <Input
          id={`${field}-${inboxId}`}
          type="password"
          placeholder={`New ${label.toLowerCase()}…`}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={status === "loading" || status === "success"}
          autoFocus
        />
      </div>
      <Button
        type="submit"
        size="sm"
        disabled={!value.trim() || status === "loading" || status === "success"}
      >
        {status === "loading"
          ? "Saving…"
          : status === "success"
            ? "Saved ✓"
            : "Save"}
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => {
          setOpen(false);
          setValue("");
          setStatus("idle");
          setErrorMsg(null);
        }}
        disabled={status === "loading"}
      >
        Cancel
      </Button>
      {status === "error" && errorMsg && (
        <p className="text-xs text-destructive">{errorMsg}</p>
      )}
    </form>
  );
}

interface ConfigRowProps {
  label: string;
  value: string | number | boolean | null | undefined;
}

function ConfigRow({ label, value }: ConfigRowProps) {
  const display =
    value === null || value === undefined || value === ""
      ? "—"
      : typeof value === "boolean"
        ? value
          ? "yes"
          : "no"
        : String(value);

  return (
    <div className="flex items-baseline justify-between gap-4 py-1 text-sm">
      <span className="min-w-0 text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <span className="font-mono text-xs text-foreground">{display}</span>
    </div>
  );
}

interface EmailInboxCardProps {
  tenantId: string;
  inbox: EmailInboxConfig;
}

function EmailInboxCard({ tenantId, inbox }: EmailInboxCardProps) {
  return (
    <Card className="p-4 space-y-4">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">
            {inbox.inbox_name ?? `Inbox #${inbox.inbox_id}`}
          </p>
          {inbox.email && (
            <p className="text-xs text-muted-foreground">{inbox.email}</p>
          )}
        </div>
        <Badge variant="outline" className="text-xs">
          #{inbox.inbox_id}
        </Badge>
      </div>

      {/* IMAP */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 mb-1">
          <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            IMAP
          </p>
          <Badge
            variant="outline"
            className={
              inbox.imap_enabled
                ? "border-success/30 bg-success/15 text-success text-xs"
                : "text-xs"
            }
          >
            {inbox.imap_enabled ? "enabled" : "disabled"}
          </Badge>
        </div>
        <div className="rounded-md border border-border bg-muted/30 px-3 py-2 space-y-0.5">
          <ConfigRow label="Address" value={inbox.imap_address} />
          <ConfigRow label="Port" value={inbox.imap_port} />
          <ConfigRow label="Login" value={inbox.imap_login} />
          <ConfigRow label="SSL" value={inbox.imap_enable_ssl} />
          <ConfigRow label="Auth" value={inbox.imap_authentication} />
        </div>
        <PasswordUpdatePanel
          tenantId={tenantId}
          inboxId={inbox.inbox_id}
          field="imap_password"
          label="IMAP Password"
        />
      </div>

      {/* SMTP */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 mb-1">
          <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            SMTP
          </p>
          <Badge
            variant="outline"
            className={
              inbox.smtp_enabled
                ? "border-success/30 bg-success/15 text-success text-xs"
                : "text-xs"
            }
          >
            {inbox.smtp_enabled ? "enabled" : "disabled"}
          </Badge>
        </div>
        <div className="rounded-md border border-border bg-muted/30 px-3 py-2 space-y-0.5">
          <ConfigRow label="Address" value={inbox.smtp_address} />
          <ConfigRow label="Port" value={inbox.smtp_port} />
          <ConfigRow label="Domain" value={inbox.smtp_domain} />
          <ConfigRow label="Login" value={inbox.smtp_login} />
          <ConfigRow label="SSL/TLS" value={inbox.smtp_enable_ssl_tls} />
          <ConfigRow label="STARTTLS" value={inbox.smtp_enable_starttls_auto} />
          <ConfigRow
            label="OpenSSL verify"
            value={inbox.smtp_openssl_verify_mode}
          />
          <ConfigRow label="Auth" value={inbox.smtp_authentication} />
        </div>
        <PasswordUpdatePanel
          tenantId={tenantId}
          inboxId={inbox.inbox_id}
          field="smtp_password"
          label="SMTP Password"
        />
      </div>
    </Card>
  );
}

interface EmailConfigSectionProps {
  tenantId: string;
  inboxes: EmailInboxConfig[];
  error?: string | null;
}

export function EmailConfigSection({
  tenantId,
  inboxes,
  error,
}: EmailConfigSectionProps) {
  if (error) {
    return (
      <Card className="border-warning/30 bg-warning/5 p-4 text-sm text-warning">
        Email config unavailable: {error}
      </Card>
    );
  }

  if (inboxes.length === 0) {
    return null;
  }

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-sm font-semibold">Email Inboxes (IMAP / SMTP)</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Config is read from Chatwoot. Passwords are never returned by the API
          — use the update panels below to rotate them. Changes take effect
          immediately in Chatwoot.
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        {inboxes.map((inbox) => (
          <EmailInboxCard
            key={inbox.inbox_id}
            tenantId={tenantId}
            inbox={inbox}
          />
        ))}
      </div>
    </div>
  );
}
