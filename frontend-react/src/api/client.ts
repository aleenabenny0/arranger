// The only place that talks to the server.
//
// `openapi-fetch` over the types generated from the API's OpenAPI document
// (src/api/schema.ts, `npm run api-types`), so a wrong path, a missing body
// field or a misspelt query parameter is a compile error. Same origin by
// default; `<meta name="arranger-api">` overrides the base for a split deploy.
// The session cookie is HttpOnly and never seen here; unsafe requests carry
// the CSRF token read from the readable cookie. Every failure becomes an
// ApiError with a message fit to show.
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

const meta = typeof document !== "undefined" ? document.querySelector<HTMLMetaElement>('meta[name="arranger-api"]') : null;
export const BASE = ((meta && meta.content) || "").replace(/\/+$/, "");
// Requests are made against an absolute origin: same-origin unless the meta
// tag says otherwise. (A relative URL would do in a browser, but not in the
// Node fetch the tests run under.)
export const ORIGIN = BASE || (typeof window !== "undefined" ? window.location.origin : "");

export class ApiError extends Error {
  status: number;
  code: string;
  aborted: boolean;

  constructor(message: string, { status = 0, code = "", aborted = false }: { status?: number; code?: string; aborted?: boolean } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.aborted = aborted;
  }
}

export function cookie(name: string): string {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) return decodeURIComponent(trimmed.slice(prefix.length));
  }
  return "";
}

const FALLBACK: Record<number, string> = {
  401: "Please sign in.", 403: "You are not allowed to do that.", 404: "That was not found.",
  413: "That file is too large.", 429: "Too many requests. Wait a moment and try again.",
};

interface ErrorDetail { detail?: unknown }

export function messageFrom(status: number, payload: unknown): { message: string; code: string } {
  const detail = (payload as ErrorDetail | null)?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { detail?: unknown; error?: unknown; request_id?: unknown };
    if (typeof d.detail === "string") {
      const reference = d.request_id ? ` (reference ${String(d.request_id).slice(0, 8)})` : "";
      return { message: d.detail + reference, code: String(d.error || "") };
    }
  }
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { loc?: unknown; msg?: unknown };
    const where = Array.isArray(first.loc) ? first.loc.filter((p) => p !== "body").join(" › ") : "";
    return { message: `${where ? `${where}: ` : ""}${typeof first.msg === "string" ? first.msg : "Invalid value."}`, code: "invalid_input" };
  }
  if (typeof detail === "string") return { message: detail, code: "" };
  return { message: FALLBACK[status] || `The server could not complete the request (${status}).`, code: "" };
}

const NETWORK_MESSAGE = "Could not reach the server. Check your connection and try again.";

// Every request: JSON accept header and, on unsafe methods, the CSRF token.
// Every error response: an ApiError thrown with a message for the user.
const middleware: Middleware = {
  async onRequest({ request }) {
    request.headers.set("Accept", "application/json");
    if (!["GET", "HEAD"].includes(request.method)) {
      const token = cookie("arranger_csrf");
      if (token) request.headers.set("X-CSRF-Token", token);
    }
    return request;
  },
  async onResponse({ response }) {
    if (response.ok) return response;
    let payload: unknown = null;
    if ((response.headers.get("content-type") || "").includes("application/json")) {
      try { payload = await response.clone().json(); } catch { payload = null; }
    }
    const { message, code } = messageFrom(response.status, payload);
    throw new ApiError(message, { status: response.status, code });
  },
  async onError({ error }) {
    if (error instanceof ApiError) throw error;
    if (error && (error as { name?: string }).name === "AbortError") throw new ApiError("Cancelled.", { aborted: true });
    throw new ApiError(NETWORK_MESSAGE);
  },
};

export function makeClient(baseUrl: string = BASE, fetchImpl?: typeof fetch) {
  // Late-bound: tests replace the global fetch after this module is loaded.
  const late: typeof fetch = (input, init) => globalThis.fetch(input, init);
  const client = createClient<paths>({ baseUrl: baseUrl || ORIGIN || undefined, credentials: baseUrl ? "include" : "same-origin", fetch: fetchImpl || late });
  client.use(middleware);
  return client;
}

export const client = makeClient();

// Raw fetches for what the typed client does not model: uploads as a request
// body, MusicXML text, and download links.
async function rawFetch(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers || {});
  if (!["GET", "HEAD"].includes((init.method || "GET").toUpperCase())) {
    const token = cookie("arranger_csrf");
    if (token) headers.set("X-CSRF-Token", token);
  }
  let response: Response;
  try {
    response = await fetch(`${ORIGIN}${path}`, { ...init, headers, credentials: BASE ? "include" : "same-origin" });
  } catch (error) {
    if (error && (error as { name?: string }).name === "AbortError") throw new ApiError("Cancelled.", { aborted: true });
    throw new ApiError(NETWORK_MESSAGE);
  }
  if (!response.ok) {
    let payload: unknown = null;
    if ((response.headers.get("content-type") || "").includes("application/json")) {
      try { payload = await response.json(); } catch { payload = null; }
    }
    const { message, code } = messageFrom(response.status, payload);
    throw new ApiError(message, { status: response.status, code });
  }
  return response;
}

export async function uploadFile(file: File, { signal, idempotencyKey }: { signal?: AbortSignal; idempotencyKey?: string } = {}): Promise<unknown> {
  const url = new URL(`${BASE}/projects/import`, window.location.origin);
  url.searchParams.set("filename", file.name);
  const headers: Record<string, string> = { Accept: "application/json", "Content-Type": "application/octet-stream" };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const response = await rawFetch(url.pathname + url.search, { method: "POST", body: file, headers, signal });
  return response.json();
}

export async function fetchText(path: string, signal?: AbortSignal): Promise<string> {
  return (await rawFetch(path, { method: "GET", headers: { Accept: "*/*" }, signal })).text();
}

export function downloadUrl(artifactId: string): string {
  return `${BASE}/artifacts/${encodeURIComponent(artifactId)}/download`;
}

export interface Job {
  id: string;
  kind: string;
  status: string;
  project_id: string | null;
  progress: number;
  stage: string;
  attempts: number;
  max_attempts: number;
  cancel_requested: boolean;
  result: Record<string, unknown> | null;
  error: { code: string; message: string } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export async function getJob(jobId: string, signal?: AbortSignal): Promise<Job> {
  const { data } = await client.GET("/jobs/{job_id}", { params: { path: { job_id: jobId } }, signal });
  return (data as { job: Job }).job;
}

// Poll a job until it ends. Backs off, stops on abort, reports progress.
export async function watchJob(jobId: string, { onUpdate, signal }: { onUpdate?: (job: Job) => void; signal?: AbortSignal } = {}): Promise<Job> {
  let delay = 400;
  for (;;) {
    const job = await getJob(jobId, signal);
    if (onUpdate) onUpdate(job);
    if (["succeeded", "failed", "cancelled"].includes(job.status)) return job;
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, delay);
      if (signal) signal.addEventListener("abort", () => { window.clearTimeout(timer); reject(new ApiError("Cancelled.", { aborted: true })); }, { once: true });
    });
    delay = Math.min(2500, Math.round(delay * 1.4));
  }
}
