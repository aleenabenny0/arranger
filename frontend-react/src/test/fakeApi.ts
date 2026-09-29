// A fetch stand-in for tests: routes matched by method and path, responses
// given as JSON, text, or a status with an error body. Every call is recorded.
import { vi } from "vitest";

export interface Call { method: string; path: string; headers: Headers; body: unknown }

type Handler = (call: Call, url: URL) => Response | Promise<Response>;

export function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

export function textResponse(status: number, body: string, contentType = "text/plain"): Response {
  return new Response(body, { status, headers: { "content-type": contentType } });
}

export function apiError(status: number, detail: string, code = "error"): Response {
  return jsonResponse(status, { detail: { detail, error: code, request_id: "req-1234567890" } });
}

export class FakeApi {
  calls: Call[] = [];
  private routes: { method: string; pattern: RegExp; handler: Handler }[] = [];

  // A handler function, a Response to replay, or a JSON body to answer with 200.
  on(method: string, path: string | RegExp, handler: Handler): this;
  on(method: string, path: string | RegExp, response: Response): this;
  on(method: string, path: string | RegExp, body: unknown): this;
  on(method: string, path: string | RegExp, handler: Handler | Response | unknown): this {
    const pattern = path instanceof RegExp ? path : new RegExp(`^${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);
    const fn: Handler = typeof handler === "function" ? (handler as Handler) : handler instanceof Response ? () => (handler as Response).clone() : () => jsonResponse(200, handler);
    this.routes.push({ method: method.toUpperCase(), pattern, handler: fn });
    return this;
  }

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = input instanceof Request ? input : new Request(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    const url = new URL(request.url, "http://localhost");
    let body: unknown = null;
    const raw = await request.clone().text().catch(() => "");
    if (raw) { try { body = JSON.parse(raw); } catch { body = raw; } }
    const call: Call = { method: request.method, path: url.pathname, headers: request.headers, body };
    this.calls.push(call);
    for (const route of this.routes) {
      if (route.method === call.method && route.pattern.test(url.pathname)) return route.handler(call, url);
    }
    return jsonResponse(404, { detail: `no fake route for ${call.method} ${url.pathname}` });
  };

  install(): this {
    vi.stubGlobal("fetch", this.fetch);
    return this;
  }

  sent(method: string, path: string): Call[] {
    return this.calls.filter((c) => c.method === method.toUpperCase() && c.path === path);
  }
}

export function installApi(): FakeApi {
  return new FakeApi().install();
}
