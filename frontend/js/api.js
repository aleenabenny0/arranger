// The only place that talks to the server.
//
// - Same-origin by default. The API base can be overridden with
//   <meta name="arranger-api" content="https://api.example"> for a split deploy.
// - The session cookie is HttpOnly; this code never sees it. Unsafe requests
//   carry the CSRF token read from the readable cookie.
// - Every failure becomes an ApiError with a message fit to show. Server text
//   is only ever rendered with textContent.

const meta = document.querySelector('meta[name="arranger-api"]');
const BASE = ((meta && meta.content) || "").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message, { status = 0, code = "", aborted = false } = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.aborted = aborted;
  }
}

function cookie(name) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) return decodeURIComponent(trimmed.slice(prefix.length));
  }
  return "";
}

export function newIdempotencyKey() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function messageFrom(status, payload) {
  const detail = payload && payload.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && typeof detail.detail === "string") {
    const reference = detail.request_id ? ` (reference ${String(detail.request_id).slice(0, 8)})` : "";
    return { message: detail.detail + reference, code: String(detail.error || "") };
  }
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0];
    const where = Array.isArray(first.loc) ? first.loc.filter((p) => p !== "body").join(" › ") : "";
    return { message: `${where ? `${where}: ` : ""}${first.msg || "Invalid value."}`, code: "invalid_input" };
  }
  if (typeof detail === "string") return { message: detail, code: "" };
  const fallback = {
    401: "Please sign in.", 403: "You are not allowed to do that.", 404: "That was not found.",
    413: "That file is too large.", 429: "Too many requests. Wait a moment and try again.",
  };
  return { message: fallback[status] || `The server could not complete the request (${status}).`, code: "" };
}

export async function request(method, path, { json, body, query, signal, headers = {}, idempotencyKey, raw = false } = {}) {
  const url = new URL(BASE + path, window.location.origin);
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  const init = { method, credentials: BASE ? "include" : "same-origin", signal, headers: { Accept: "application/json", ...headers } };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers["Content-Type"] = "application/json";
  } else if (body !== undefined) {
    init.body = body;
    init.headers["Content-Type"] = "application/octet-stream";
  }
  if (!["GET", "HEAD"].includes(method)) {
    const token = cookie("arranger_csrf");
    if (token) init.headers["X-CSRF-Token"] = token;
  }
  if (idempotencyKey) init.headers["Idempotency-Key"] = idempotencyKey;

  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    if (error && error.name === "AbortError") throw new ApiError("Cancelled.", { aborted: true });
    throw new ApiError("Could not reach the server. Check your connection and try again.");
  }
  if (raw && response.ok) return response;
  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    try { payload = await response.json(); } catch { payload = null; }
  }
  if (!response.ok) {
    const { message, code } = messageFrom(response.status, payload);
    throw new ApiError(message, { status: response.status, code });
  }
  return payload;
}

export const api = {
  get: (path, options) => request("GET", path, options),
  post: (path, json, options) => request("POST", path, { json: json === undefined ? {} : json, ...options }),
  patch: (path, json, options) => request("PATCH", path, { json, ...options }),
  del: (path, json, options) => request("DELETE", path, { json, ...options }),
  upload: (path, file, query, options) => request("POST", path, { body: file, query, ...options }),
  text: async (path, options) => (await request("GET", path, { ...options, raw: true, headers: { Accept: "*/*" } })).text(),
  downloadUrl: (artifactId) => `${BASE}/artifacts/${encodeURIComponent(artifactId)}/download`,
};

// Poll a job until it ends. Backs off, stops on abort, and reports progress.
export async function watchJob(jobId, { onUpdate, signal } = {}) {
  let delay = 400;
  for (;;) {
    const { job } = await api.get(`/jobs/${encodeURIComponent(jobId)}`, { signal });
    if (onUpdate) onUpdate(job);
    if (["succeeded", "failed", "cancelled"].includes(job.status)) return job;
    await new Promise((resolve, reject) => {
      const timer = window.setTimeout(resolve, delay);
      if (signal) signal.addEventListener("abort", () => { window.clearTimeout(timer); reject(new ApiError("Cancelled.", { aborted: true })); }, { once: true });
    });
    delay = Math.min(2500, Math.round(delay * 1.4));
  }
}
