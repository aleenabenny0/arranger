import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, cookie, fetchText, getJob, makeClient, messageFrom, uploadFile, watchJob } from "./client";
import { apiError, installApi, jsonResponse, textResponse } from "../test/fakeApi";

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "arranger_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

async function failureOf(work: Promise<unknown>): Promise<ApiError> {
  try {
    await work;
  } catch (error) {
    return error as ApiError;
  }
  throw new Error("the request did not fail");
}

describe("messageFrom", () => {
  it("uses the server's message and code, with a short reference", () => {
    const { message, code } = messageFrom(403, { detail: { detail: "Verify your email first.", error: "email_unverified", request_id: "abcdef123456" } });
    expect(message).toBe("Verify your email first. (reference abcdef12)");
    expect(code).toBe("email_unverified");
  });

  it("names the field for a validation error", () => {
    const { message, code } = messageFrom(422, { detail: [{ loc: ["body", "profile", "max_span"], msg: "must be at most 24" }] });
    expect(message).toBe("profile › max_span: must be at most 24");
    expect(code).toBe("invalid_input");
  });

  it("falls back to a plain sentence by status", () => {
    expect(messageFrom(429, null).message).toMatch(/Too many requests/);
    expect(messageFrom(418, null).message).toBe("The server could not complete the request (418).");
    expect(messageFrom(400, { detail: "Bad file." }).message).toBe("Bad file.");
  });
});

describe("cookie", () => {
  it("reads one cookie by name", () => {
    document.cookie = "other=1";
    document.cookie = "arranger_csrf=tok%20en";
    expect(cookie("arranger_csrf")).toBe("tok en");
    expect(cookie("missing")).toBe("");
  });
});

describe("the typed client", () => {
  it("sends the CSRF token on unsafe requests and not on GET", async () => {
    document.cookie = "arranger_csrf=secret";
    const api = installApi().on("GET", "/auth/me", { user: { id: "u1" } }).on("POST", "/auth/logout", {});
    const client = makeClient("");
    await client.GET("/auth/me");
    await client.POST("/auth/logout");
    expect(api.sent("GET", "/auth/me")[0].headers.get("x-csrf-token")).toBeNull();
    expect(api.sent("POST", "/auth/logout")[0].headers.get("x-csrf-token")).toBe("secret");
    expect(api.sent("POST", "/auth/logout")[0].headers.get("accept")).toBe("application/json");
  });

  it("turns an error response into an ApiError with the server's words", async () => {
    installApi().on("POST", "/auth/login", apiError(401, "Wrong email or password.", "bad_credentials"));
    const client = makeClient("");
    const failure = await failureOf(client.POST("/auth/login", { body: { email: "a@b.co", password: "x" } }));
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.status).toBe(401);
    expect(failure.code).toBe("bad_credentials");
    expect(failure.message).toMatch(/^Wrong email or password\./);
  });

  it("explains a non-JSON error body by status", async () => {
    installApi().on("GET", "/catalog", textResponse(503, "<html>down</html>", "text/html"));
    const failure = await failureOf(makeClient("").GET("/catalog"));
    expect(failure.message).toBe("The server could not complete the request (503).");
  });

  it("reports a network failure in words", async () => {
    vi.stubGlobal("fetch", async () => { throw new TypeError("Failed to fetch"); });
    const failure = await failureOf(makeClient("").GET("/catalog"));
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.message).toMatch(/Could not reach the server/);
    expect(failure.aborted).toBe(false);
  });

  it("marks an aborted request so callers can stay quiet", async () => {
    vi.stubGlobal("fetch", async () => { throw new DOMException("Aborted", "AbortError"); });
    const failure = await failureOf(makeClient("").GET("/catalog"));
    expect(failure.aborted).toBe(true);
  });
});

describe("raw helpers", () => {
  it("uploads a file as the request body with its name in the query", async () => {
    document.cookie = "arranger_csrf=secret";
    const api = installApi().on("POST", "/projects/import", (_call, url) => jsonResponse(201, { project: { id: "p1", title: url.searchParams.get("filename") } }));
    const result = await uploadFile(new File(["MThd"], "tune.mid"), { idempotencyKey: "k1" }) as { project: { title: string } };
    expect(result.project.title).toBe("tune.mid");
    const call = api.sent("POST", "/projects/import")[0];
    expect(call.headers.get("idempotency-key")).toBe("k1");
    expect(call.headers.get("x-csrf-token")).toBe("secret");
    expect(call.headers.get("content-type")).toBe("application/octet-stream");
  });

  it("fetches text and raises on a failure", async () => {
    installApi().on("GET", "/x.xml", textResponse(200, "<score/>", "application/xml")).on("GET", "/y.xml", apiError(404, "No such score."));
    expect(await fetchText("/x.xml")).toBe("<score/>");
    await expect(fetchText("/y.xml")).rejects.toMatchObject({ status: 404, message: /No such score/ });
  });

  it("polls a job until it ends and reports each state", async () => {
    vi.useFakeTimers();
    let polls = 0;
    installApi().on("GET", "/jobs/j1", () => {
      polls += 1;
      const done = polls >= 3;
      return jsonResponse(200, { job: { id: "j1", status: done ? "succeeded" : "running", progress: done ? 1 : polls * 0.3, stage: done ? "done" : "arranging", result: done ? { arrangement_id: "a1" } : null } });
    });
    const seen: string[] = [];
    const promise = watchJob("j1", { onUpdate: (j) => seen.push(j.status) });
    for (let i = 0; i < 4; i += 1) {
      await vi.advanceTimersByTimeAsync(3000);
    }
    const job = await promise;
    expect(job.status).toBe("succeeded");
    expect(seen).toEqual(["running", "running", "succeeded"]);
    expect(await getJob("j1")).toMatchObject({ id: "j1" });
    vi.useRealTimers();
  });
});
