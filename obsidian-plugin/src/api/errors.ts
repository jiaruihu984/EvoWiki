export class ApiError extends Error {
  /** `code`: the response's machine code when it sent one (e.g. `llm_busy`, `patches_off`); branch on it, not on the text. */
  constructor(readonly status: number, readonly detail: string, readonly code: string | null = null) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }
}

/** The error of a non-2xx response: its detail (detailOf) and its JSON `code` when there is one. */
export function apiErrorOf(status: number, text: string): ApiError {
  return new ApiError(status, detailOf(status, text), codeOf(text));
}

/** A JSON body's string `code`, else null. */
export function codeOf(text: string): string | null {
  try {
    const c = (JSON.parse(text) as { code?: unknown } | null)?.code;
    return typeof c === "string" && c !== "" ? c : null;
  } catch { return null; }
}

/** The server could not be reached at all (connection refused, DNS, reset). */
export class OfflineError extends Error {
  constructor(readonly reason?: unknown) {
    super("EvoWiki server unreachable");
    this.name = "OfflineError";
  }
}

/** A request the plugin cancelled itself (note switched, plugin unloaded). */
export class AbortedError extends Error {
  constructor() { super("aborted"); this.name = "AbortedError"; }
}

/**
 * A 422 detail for people: FastAPI sends a list of {loc, msg, type}; show the joined `msg`s without pydantic's
 * "Value error, " prefix. Anything else (a plain string, a list without msgs) is returned as is.
 */
export function validationText(detail: string): string {
  try {
    const d: unknown = JSON.parse(detail);
    if (Array.isArray(d)) {
      const msgs = d.flatMap((x: unknown) => {
        const m = (x as { msg?: unknown } | null)?.msg;
        return typeof m === "string" ? [m.replace(/^Value error, /, "")] : [];
      });
      if (msgs.length > 0) return msgs.join("; ");
    }
  } catch { /* not JSON */ }
  return detail;
}

export function detailOf(status: number, text: string): string {
  try {
    const d = (JSON.parse(text) as { detail?: unknown } | null)?.detail;
    if (typeof d === "string") return d;
    if (d !== undefined) return JSON.stringify(d);
  } catch { /* not JSON */ }
  return text || `HTTP ${status}`;
}
