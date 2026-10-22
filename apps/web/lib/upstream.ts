// What the server-side proxy may hand to the browser from an upstream answer (reliability
// hardening, 2026-10-22). The API always answers JSON; anything else — a tunnel or CDN error
// page, a captive portal, an empty reply, a connection failure — is an infrastructure
// failure and becomes one predictable application error, never raw HTML, upstream URLs or
// stack traces. Real API answers (any status, JSON body) pass through unchanged.

export const UNAVAILABLE_STATUS = 502;
export const UNAVAILABLE_DETAIL = "API unavailable";
export const UNAVAILABLE_CODE = "upstream_unavailable";

export interface UpstreamResult {
  status: number;
  /** the exact JSON text to forward, or the replacement error body */
  body: string;
  contentType: string;
  /** true when the answer was replaced by the unavailable error */
  replaced: boolean;
}

export function unavailableBody(detail: string = UNAVAILABLE_DETAIL): string {
  return JSON.stringify({ detail, code: UNAVAILABLE_CODE });
}

function isJsonObjectOrArray(text: string): boolean {
  const t = text.trim();
  if (!(t.startsWith("{") || t.startsWith("["))) return false;
  try {
    JSON.parse(t);
    return true;
  } catch {
    return false;
  }
}

/** Decide what to forward. The API's own answers pass through unchanged: JSON with any
 * status, and other media it serves on a success (report charts as SVG). What is replaced by
 * the unavailable error is what only an intermediary produces: an HTML page (a tunnel, CDN or
 * proxy error page, a captive portal) at any status, any non-JSON body on a 5xx, a body
 * declared JSON that does not parse, and an empty body on a non-success. */
export function classifyUpstream(status: number, contentType: string | null, text: string): UpstreamResult {
  const ct = (contentType ?? "").toLowerCase();
  const replaced = { status: UNAVAILABLE_STATUS, body: unavailableBody(), contentType: "application/json", replaced: true };
  if (text.trim() === "") {
    if (status >= 200 && status < 300) return { status, body: "", contentType: ct || "application/json", replaced: false };
    return replaced;
  }
  if (ct.includes("text/html")) return replaced;
  if (ct.includes("application/json")) {
    return isJsonObjectOrArray(text) ? { status, body: text, contentType: "application/json", replaced: false } : replaced;
  }
  if (!ct && isJsonObjectOrArray(text)) return { status, body: text, contentType: "application/json", replaced: false };
  // other media (e.g. image/svg+xml from the API's reports) is only legitimate on a success
  if (status < 500) return { status, body: text, contentType: ct || "application/octet-stream", replaced: false };
  return replaced;
}
