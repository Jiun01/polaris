/**
 * Polaris API error envelope (Phase 9 PR-3).
 *
 * Mirrors the API's standard error shape (05 §7). The client/server wrappers
 * throw ApiError on any non-2xx response; callers can pattern-match on
 * `error.status` and the envelope code to drive UX.
 *
 * `apiErrorFromResponse` parses the four shapes the API can emit — the
 * standard `{ error: {...} }` envelope, FastAPI validation errors
 * (`{ detail: [...] }`), hand-raised `HTTPException(detail="...")` strings,
 * and non-JSON bodies — so admin surfaces a useful message instead of a
 * generic "HTTP 4xx" string. (Ported from apps/portal/lib/api/errors.ts.)
 */

export interface PolarisErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
    request_id?: string;
    documentation_url?: string;
  };
}

export class ApiError extends Error {
  status: number;
  envelope?: PolarisErrorEnvelope;

  constructor(status: number, envelope?: PolarisErrorEnvelope) {
    const msg =
      envelope?.error?.message ?? `Polaris API returned HTTP ${status}`;
    super(msg);
    this.name = "ApiError";
    this.status = status;
    this.envelope = envelope;
  }

  /** Convenience: the envelope's `code` ("not_found", "quota_exceeded", …). */
  get code(): string | undefined {
    return this.envelope?.error?.code;
  }

  /** Structured error details, when the envelope carries them. */
  get details(): Record<string, unknown> | undefined {
    return this.envelope?.error?.details;
  }

  /** Request id for cross-referencing API logs, when present. */
  get requestId(): string | undefined {
    return this.envelope?.error?.request_id;
  }

  /** Documentation deep-link, when the envelope carries one. */
  get documentationUrl(): string | undefined {
    return this.envelope?.error?.documentation_url;
  }
}

/**
 * Parse a non-OK Response into a typed ApiError. Falls back to a synthetic
 * envelope if the body isn't the standard shape (FastAPI validation errors,
 * hand-raised HTTPException strings, or a non-JSON 5xx HTML error page).
 */
export async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  let inner: PolarisErrorEnvelope["error"];
  try {
    const body: unknown = await res.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "error" in body &&
      typeof (body as PolarisErrorEnvelope).error === "object"
    ) {
      inner = (body as PolarisErrorEnvelope).error;
    } else if (
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      Array.isArray((body as { detail: unknown }).detail)
    ) {
      // FastAPI validation errors: { detail: [{ loc, msg, type }] }
      type FastApiError = { msg?: string; loc?: unknown[] };
      const detail = (body as { detail: FastApiError[] }).detail;
      const firstMsg = detail[0]?.msg ?? "Validation failed";
      inner = {
        code: "VALIDATION_ERROR",
        message: firstMsg,
        details: { errors: detail as Record<string, unknown>[] },
      };
    } else if (
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      // Hand-raised HTTPException(status, detail="...") — FastAPI serialises the
      // string straight into `detail`. The backend already supplies a readable
      // message, so surface it instead of the generic HTTP fallback.
      inner = {
        code: "VALIDATION_ERROR",
        message: (body as { detail: string }).detail,
      };
    } else {
      inner = {
        code: "POLARIS_UNEXPECTED_RESPONSE",
        message: `HTTP ${res.status} from Polaris API`,
      };
    }
  } catch {
    inner = {
      code: "POLARIS_UNPARSEABLE_RESPONSE",
      message: `HTTP ${res.status} from Polaris API (non-JSON body)`,
    };
  }
  return new ApiError(res.status, { error: inner });
}
