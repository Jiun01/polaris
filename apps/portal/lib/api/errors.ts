/**
 * Polaris API error envelope (Phase 8 PR-3).
 *
 * Mirrors `docs/Polaris V2/05_DATA_MODEL_AND_API.md §7` — every API
 * surface emits errors as `{ error: { code, message, details, request_id, documentation_url } }`.
 * Client + server fetch wrappers parse this shape into a typed exception
 * so consumer code can branch on `code` without re-parsing JSON.
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

export class PolarisApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;
  readonly requestId?: string;
  readonly documentationUrl?: string;

  constructor(
    status: number,
    envelope: PolarisErrorEnvelope["error"],
    cause?: unknown,
  ) {
    super(envelope.message);
    this.name = "PolarisApiError";
    this.status = status;
    this.code = envelope.code;
    this.details = envelope.details;
    this.requestId = envelope.request_id;
    this.documentationUrl = envelope.documentation_url;
    if (cause !== undefined) (this as { cause?: unknown }).cause = cause;
  }
}

/**
 * Parse a non-OK Response into a typed PolarisApiError. Falls back to a
 * synthetic envelope if the body isn't JSON (e.g. CORS preflight failure,
 * 5xx HTML error page from upstream).
 */
export async function polarisErrorFromResponse(
  res: Response,
): Promise<PolarisApiError> {
  let envelope: PolarisErrorEnvelope["error"];
  try {
    const body: unknown = await res.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "error" in body &&
      typeof (body as PolarisErrorEnvelope).error === "object"
    ) {
      envelope = (body as PolarisErrorEnvelope).error;
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
      envelope = {
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
      // message (e.g. the widget allowlist error), so surface it instead of the
      // generic "HTTP 422 from Polaris API" fallback (QA finding L7).
      envelope = {
        code: "VALIDATION_ERROR",
        message: (body as { detail: string }).detail,
      };
    } else {
      envelope = {
        code: "POLARIS_UNEXPECTED_RESPONSE",
        message: `HTTP ${res.status} from Polaris API`,
      };
    }
  } catch {
    envelope = {
      code: "POLARIS_UNPARSEABLE_RESPONSE",
      message: `HTTP ${res.status} from Polaris API (non-JSON body)`,
    };
  }
  return new PolarisApiError(res.status, envelope);
}
