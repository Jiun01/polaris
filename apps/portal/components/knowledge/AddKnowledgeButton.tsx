"use client";

/**
 * "Add knowledge" CTA + dialog wrapper (Phase 8 PR-6 / file-upload follow-up).
 *
 * Three submission paths (all POST to /sources — declarative desired-state):
 *   - URL:  POST {source_type: "url", source_url, source_label, desired_hash}
 *   - Text: POST {source_type: "text", source_text, source_label, desired_hash}
 *   - File: sign upload → PUT bytes → POST {source_type: "file", storage_path,
 *             source_label, desired_hash}
 *
 * desired_hash is computed in-browser via Web Crypto (SHA-256):
 *   - text: SHA-256 hex of the text body.
 *   - url:  SHA-256 hex of the trimmed URL string.
 *   - file: SHA-256 hex of the file BYTES (computed before the sign step).
 *
 * File upload uses the three-step flow:
 *   1. POST /uploads/sign  → upload_url + storage_path
 *   2. PUT raw bytes       → Supabase Storage signed URL
 *   3. POST /sources       → declare source with storage_path
 *
 * Capacity (409) and storage-unavailable (503) errors are surfaced with
 * human-readable messages. On success, router.refresh() re-fetches the
 * Sources table without a full page reload.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { cn } from "@/lib/utils";

// 50 MiB — same as the backend cap; client-side check saves a round-trip.
const MAX_FILE_BYTES = 52_428_800;

// Allowed MIME types per the backend's `ALLOWED_CONTENT_TYPES` set.
const ACCEPT_MIME = [
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.oasis.opendocument.text",
  "application/vnd.oasis.opendocument.spreadsheet",
  "application/vnd.oasis.opendocument.presentation",
  "text/*",
  "image/*",
].join(",");

type Tab = "url" | "text" | "file";

const TABS: { value: Tab; label: string }[] = [
  { value: "url", label: "URL" },
  { value: "text", label: "Text" },
  { value: "file", label: "File" },
];

interface SignUploadResponse {
  upload_url: string;
  storage_path: string;
  expires_in_seconds: number;
}

interface CapacityExceededError {
  error: "kb_capacity_exceeded";
  current_mb: number;
  tier_cap_mb: number;
  would_be_mb: number;
  hint: string;
}

type UploadPhase = "idle" | "signing" | "uploading" | "queuing" | "done";

// ---------------------------------------------------------------------------
// SHA-256 helper (Web Crypto API — secure contexts only; fine for the portal)
// ---------------------------------------------------------------------------
async function sha256Hex(data: ArrayBuffer | string): Promise<string> {
  const buf =
    typeof data === "string" ? new TextEncoder().encode(data).buffer : data;
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// ---------------------------------------------------------------------------
// Label defaults
// ---------------------------------------------------------------------------
function defaultLabelForUrl(raw: string): string {
  try {
    return new URL(raw).hostname || raw.slice(0, 40);
  } catch {
    return raw.slice(0, 40) || "URL source";
  }
}

function defaultLabelForText(raw: string): string {
  const trimmed = raw.trim().slice(0, 40);
  return trimmed || "Pasted text";
}

export function AddKnowledgeButton() {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [tab, setTab] = React.useState<Tab>("url");

  // URL tab state
  const [url, setUrl] = React.useState("");
  const [urlLabel, setUrlLabel] = React.useState("");

  // Text tab state
  const [text, setText] = React.useState("");
  const [textLabel, setTextLabel] = React.useState("");

  // File tab state
  const [file, setFile] = React.useState<File | null>(null);
  const [fileLabel, setFileLabel] = React.useState("");
  const [uploadPhase, setUploadPhase] = React.useState<UploadPhase>("idle");
  const [uploadProgress, setUploadProgress] = React.useState(0);

  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const fileInputRef = React.useRef<HTMLInputElement>(null);

  function reset() {
    setUrl("");
    setUrlLabel("");
    setText("");
    setTextLabel("");
    setFile(null);
    setFileLabel("");
    setUploadPhase("idle");
    setUploadProgress(0);
    setError(null);
    setTab("url");
  }

  // -------------------------------------------------------------------------
  // URL submit
  // -------------------------------------------------------------------------
  async function submitUrl() {
    const trimmedUrl = url.trim();
    if (!trimmedUrl) return;
    setSubmitting(true);
    setError(null);
    try {
      const label = urlLabel.trim() || defaultLabelForUrl(trimmedUrl);
      const desired_hash = await sha256Hex(trimmedUrl);
      await apiFetch(TenantRoutes.knowledgeSources(), {
        method: "POST",
        body: JSON.stringify({
          source_type: "url",
          source_url: trimmedUrl,
          source_label: label,
          desired_hash,
        }),
      });
      setOpen(false);
      reset();
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Submission failed. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  // -------------------------------------------------------------------------
  // Text submit
  // -------------------------------------------------------------------------
  async function submitText() {
    if (!text.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const label = textLabel.trim() || defaultLabelForText(text);
      const desired_hash = await sha256Hex(text);
      await apiFetch(TenantRoutes.knowledgeSources(), {
        method: "POST",
        body: JSON.stringify({
          source_type: "text",
          source_text: text,
          source_label: label,
          desired_hash,
        }),
      });
      setOpen(false);
      reset();
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Submission failed. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  // -------------------------------------------------------------------------
  // File submit — three-step: sign → PUT → POST /sources
  // -------------------------------------------------------------------------
  async function submitFile() {
    if (!file) return;

    // Client-side gate so the user gets instant feedback without a round-trip.
    if (file.size > MAX_FILE_BYTES) {
      setError(
        `File is too large (${(file.size / 1_048_576).toFixed(1)} MiB). Maximum is 50 MiB.`,
      );
      return;
    }

    setSubmitting(true);
    setError(null);

    // Compute desired_hash from the file bytes up front so we have it ready
    // for the final POST without reading the file a second time.
    let desired_hash: string;
    let fileBytes: ArrayBuffer;
    try {
      fileBytes = await file.arrayBuffer();
      desired_hash = await sha256Hex(fileBytes);
    } catch {
      setError("Could not read file for hashing. Try again.");
      setSubmitting(false);
      return;
    }

    const label = fileLabel.trim() || file.name;

    // ---- Step 1: request a signed upload URL ----
    setUploadPhase("signing");
    let signedData: SignUploadResponse;
    try {
      const result = await apiFetch<SignUploadResponse>(
        TenantRoutes.knowledgeSignUpload(),
        {
          method: "POST",
          body: JSON.stringify({
            file_size_bytes: file.size,
            content_type: file.type || "application/octet-stream",
          }),
        },
      );
      if (!result) throw new Error("Empty response from sign endpoint");
      signedData = result;
    } catch (err) {
      if (err instanceof PolarisApiError) {
        if (err.status === 409) {
          // Capacity exceeded — parse the structured details.
          const d = err.details as Partial<CapacityExceededError> | undefined;
          const hint = d?.hint ?? "";
          const used = d?.current_mb != null ? `${d.current_mb} MB` : "";
          const cap = d?.tier_cap_mb != null ? `${d.tier_cap_mb} MB` : "";
          setError(
            `Knowledge storage full${used && cap ? ` (${used} / ${cap})` : ""}. ${hint || "Delete unused sources or upgrade your plan to add more."}`,
          );
        } else if (err.status === 503) {
          setError(
            "Storage is temporarily unavailable. Please try again in a few minutes.",
          );
        } else {
          setError(err.message || "Could not prepare upload. Try again.");
        }
      } else {
        setError("Could not prepare upload. Try again.");
      }
      setUploadPhase("idle");
      setSubmitting(false);
      return;
    }

    // ---- Step 2: PUT raw bytes to the signed Supabase URL ----
    setUploadPhase("uploading");
    setUploadProgress(0);
    try {
      // XMLHttpRequest gives us upload progress; fetch does not (no body-stream
      // progress API in all supported runtimes).
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", signedData.upload_url);
        xhr.setRequestHeader(
          "Content-Type",
          file.type || "application/octet-stream",
        );
        xhr.upload.addEventListener("progress", (e) => {
          if (e.lengthComputable) {
            setUploadProgress(Math.round((e.loaded / e.total) * 100));
          }
        });
        xhr.addEventListener("load", () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            setUploadProgress(100);
            resolve();
          } else {
            reject(new Error(`Storage PUT failed: ${xhr.status}`));
          }
        });
        xhr.addEventListener("error", () =>
          reject(new Error("Network error during file upload")),
        );
        xhr.send(file);
      });
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "File upload failed. Check your connection and try again.",
      );
      setUploadPhase("idle");
      setSubmitting(false);
      return;
    }

    // ---- Step 3: POST declarative source row ----
    setUploadPhase("queuing");
    try {
      await apiFetch(TenantRoutes.knowledgeSources(), {
        method: "POST",
        body: JSON.stringify({
          source_type: "file",
          storage_path: signedData.storage_path,
          source_label: label,
          desired_hash,
        }),
      });
      setUploadPhase("done");
      setOpen(false);
      reset();
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "File was uploaded but saving the source failed. Contact support if the source does not appear.",
      );
      setUploadPhase("idle");
    } finally {
      setSubmitting(false);
    }
  }

  // -------------------------------------------------------------------------
  // Upload phase label for the progress announcement
  // -------------------------------------------------------------------------
  const phaseLabel: Record<UploadPhase, string> = {
    idle: "",
    signing: "Preparing upload…",
    uploading:
      uploadProgress < 100 ? `Uploading… ${uploadProgress}%` : "Finalising…",
    queuing: "Saving source…",
    done: "Done.",
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        // Block close while an upload is in flight to prevent orphaned
        // storage objects without a corresponding source row.
        if (!v && submitting) return;
        setOpen(v);
        if (!v) reset();
      }}
    >
      <DialogTrigger asChild>
        <Button>
          <Plus className="mr-1.5 h-4 w-4" aria-hidden />
          Add knowledge
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add a knowledge source</DialogTitle>
          <DialogDescription>
            Your bot will use this content when answering customer questions.
            Your bot will use this after the next knowledge sync.
          </DialogDescription>
        </DialogHeader>

        {/* Tab bar */}
        <div className="flex gap-1 rounded-md bg-muted p-1" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.value}
              type="button"
              role="tab"
              aria-selected={tab === t.value}
              onClick={() => {
                setTab(t.value);
                setError(null);
              }}
              className={cn(
                "flex-1 rounded px-3 py-1.5 text-sm transition-colors",
                tab === t.value
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* URL tab */}
        {tab === "url" ? (
          <div className="space-y-3">
            <div className="space-y-2">
              <Label htmlFor="add-url">Website URL</Label>
              <Input
                id="add-url"
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://yourbusiness.com/faq"
                autoFocus
                disabled={submitting}
              />
              <p className="text-xs text-muted-foreground">
                We&apos;ll crawl the page and any same-domain links up to our
                default depth.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="add-url-label">
                Label{" "}
                <span className="text-muted-foreground font-normal">
                  (optional)
                </span>
              </Label>
              <Input
                id="add-url-label"
                type="text"
                value={urlLabel}
                onChange={(e) => setUrlLabel(e.target.value)}
                placeholder={
                  url.trim() ? defaultLabelForUrl(url.trim()) : "e.g. FAQ page"
                }
                disabled={submitting}
              />
            </div>
          </div>
        ) : null}

        {/* Text tab */}
        {tab === "text" ? (
          <div className="space-y-3">
            <div className="space-y-2">
              <Label htmlFor="add-text">Paste text</Label>
              <Textarea
                id="add-text"
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={6}
                placeholder="Paste an FAQ, policy, product list, or any text your bot should know."
                autoFocus
                disabled={submitting}
              />
              <p className="text-xs text-muted-foreground">
                Up to ~100 KB per submission. Large documents are better
                submitted as a file.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="add-text-label">
                Label{" "}
                <span className="text-muted-foreground font-normal">
                  (optional)
                </span>
              </Label>
              <Input
                id="add-text-label"
                type="text"
                value={textLabel}
                onChange={(e) => setTextLabel(e.target.value)}
                placeholder={
                  text.trim() ? defaultLabelForText(text) : "e.g. Refund policy"
                }
                disabled={submitting}
              />
            </div>
          </div>
        ) : null}

        {/* File tab */}
        {tab === "file" ? (
          <div className="space-y-3">
            <div className="space-y-2">
              <Label htmlFor="add-file">Document or image</Label>
              <Input
                ref={fileInputRef}
                id="add-file"
                type="file"
                accept={ACCEPT_MIME}
                onChange={(e) => {
                  const picked = e.target.files?.[0] ?? null;
                  setFile(picked);
                  if (picked && !fileLabel.trim()) {
                    setFileLabel(picked.name);
                  }
                  setError(null);
                  setUploadProgress(0);
                }}
                disabled={submitting}
                aria-describedby="add-file-hint"
              />
              <p id="add-file-hint" className="text-xs text-muted-foreground">
                PDF, Word, Excel, PowerPoint, OpenDocument, plain text, or
                image. Max 50 MiB.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="add-file-label">
                Label{" "}
                <span className="text-muted-foreground font-normal">
                  (optional)
                </span>
              </Label>
              <Input
                id="add-file-label"
                type="text"
                value={fileLabel}
                onChange={(e) => setFileLabel(e.target.value)}
                placeholder={file ? file.name : "e.g. Product catalogue"}
                disabled={submitting}
              />
            </div>

            {/* Upload progress bar */}
            {submitting && uploadPhase !== "idle" ? (
              <div
                role="status"
                aria-live="polite"
                aria-label={phaseLabel[uploadPhase]}
                className="space-y-1"
              >
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span>{phaseLabel[uploadPhase]}</span>
                  {uploadPhase === "uploading" ? (
                    <span className="tabular-nums">{uploadProgress}%</span>
                  ) : null}
                </div>
                <div
                  className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
                  role="progressbar"
                  aria-valuenow={
                    uploadPhase === "uploading" ? uploadProgress : undefined
                  }
                  aria-valuemin={0}
                  aria-valuemax={100}
                >
                  <div
                    className={cn(
                      "h-full rounded-full bg-primary transition-all",
                      uploadPhase !== "uploading" && "animate-pulse",
                    )}
                    style={{
                      width:
                        uploadPhase === "uploading"
                          ? `${uploadProgress}%`
                          : "100%",
                    }}
                  />
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {/* Error */}
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => setOpen(false)}
            disabled={submitting}
          >
            Cancel
          </Button>

          {tab === "url" ? (
            <Button onClick={submitUrl} disabled={submitting || !url.trim()}>
              {submitting ? "Submitting…" : "Add URL"}
            </Button>
          ) : null}

          {tab === "text" ? (
            <Button onClick={submitText} disabled={submitting || !text.trim()}>
              {submitting ? "Submitting…" : "Add text"}
            </Button>
          ) : null}

          {tab === "file" ? (
            <Button onClick={submitFile} disabled={submitting || !file}>
              {submitting
                ? phaseLabel[uploadPhase] || "Uploading…"
                : "Upload file"}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
