/**
 * Create-demo form — ADR-066.
 *
 * Single-step form (demos are simpler than the tenant create wizard):
 * brand name + trigger code + caps. On submit → POST /api/admin/demos →
 * redirect to the new demo's detail page. KB/products are added afterward
 * via "Manage in tenant view" (impersonation).
 */

"use client";

import type { ReactNode } from "react";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { ApiError } from "@/lib/api/error";
import { adminRoutes } from "@/lib/api/routes";

interface CreateResponse {
  id: string;
  slug: string;
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}

export function CreateDemoForm() {
  const router = useRouter();
  const [displayName, setDisplayName] = useState("");
  const [demoCode, setDemoCode] = useState("");
  const [durationHours, setDurationHours] = useState("6");
  const [maxMessages, setMaxMessages] = useState("50");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const valid = displayName.trim().length > 0 && demoCode.trim().length >= 3;

  async function submit() {
    setSubmitting(true);
    try {
      const res = await adminFetch<CreateResponse>(adminRoutes.demos.create(), {
        method: "POST",
        body: {
          display_name: displayName.trim(),
          demo_code: demoCode.trim(),
          duration_hours: Number(durationHours) || 6,
          max_messages: Number(maxMessages) || 50,
          description: description.trim() || null,
        },
      });
      if (!res) {
        toast.error("Demo create returned an empty response.");
        setSubmitting(false);
        return;
      }
      toast.success(`Demo "${res.slug}" created.`);
      router.push(`/demo-system/${res.id}`);
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to create demo.";
      toast.error(msg);
      setSubmitting(false);
    }
  }

  return (
    <Card className="space-y-4 p-6">
      <Field label="Brand / business name" htmlFor="display_name">
        <Input
          id="display_name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="TRG Watch"
          autoFocus
        />
      </Field>
      <Field
        label="Demo code (customers type this to start)"
        htmlFor="demo_code"
      >
        <Input
          id="demo_code"
          value={demoCode}
          onChange={(e) => setDemoCode(e.target.value)}
          placeholder="trgwatches1824"
        />
      </Field>
      <div className="grid grid-cols-2 gap-4">
        <Field label="Duration (hours)" htmlFor="duration_hours">
          <Input
            id="duration_hours"
            type="number"
            min={1}
            value={durationHours}
            onChange={(e) => setDurationHours(e.target.value)}
          />
        </Field>
        <Field label="Message cap" htmlFor="max_messages">
          <Input
            id="max_messages"
            type="number"
            min={1}
            value={maxMessages}
            onChange={(e) => setMaxMessages(e.target.value)}
          />
        </Field>
      </div>
      <Field label="Description (optional)" htmlFor="description">
        <Input
          id="description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Luxury watch retailer"
        />
      </Field>
      <div className="flex justify-end gap-2 pt-2">
        <Button
          variant="outline"
          onClick={() => router.push("/demo-system")}
          disabled={submitting}
        >
          Cancel
        </Button>
        <Button onClick={submit} disabled={!valid || submitting}>
          {submitting ? "Creating…" : "Create demo"}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        After creating, open the demo and use “Manage in tenant view” to upload
        its knowledge base + products via impersonation.
      </p>
    </Card>
  );
}
