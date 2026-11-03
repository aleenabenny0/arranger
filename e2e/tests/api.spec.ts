// The API through Playwright's request fixture: no browser, the same server.
import { expect, test } from "@playwright/test";
import { PASSWORD, uniqueEmail } from "./helpers";

test.describe("api", () => {
  test("health and readiness answer, and the catalog names the capabilities", async ({ request }) => {
    const health = await request.get("/health");
    expect(health.status()).toBe(200);
    expect(await health.json()).toMatchObject({ status: "ok", service: "arranger-api" });

    const ready = await request.get("/ready");
    expect(ready.status()).toBe(200);
    expect((await ready.json()).migrations.pending).toBe(0);

    const catalog = await request.get("/catalog");
    expect(catalog.status()).toBe(200);
    const capabilities = (await catalog.json()).capabilities;
    expect(capabilities.import_midi).toBe(true);
    expect(capabilities.export_musicxml).toBe(true);
    expect(typeof capabilities.export_pdf.available).toBe("boolean");
  });

  test("an account can register, read its profiles, and sign out with the CSRF header", async ({ request, baseURL }) => {
    expect((await request.get("/auth/me")).status()).toBe(401);

    const email = uniqueEmail("api");
    const registered = await request.post("/auth/register", { data: { email, password: PASSWORD, display_name: "API" } });
    expect(registered.status()).toBe(200);
    expect((await registered.json()).user.email).toBe(email);

    const me = await request.get("/auth/me");
    expect(me.status()).toBe(200);

    const profiles = await request.get("/profiles");
    expect(profiles.status()).toBe(200);
    expect((await profiles.json()).records).toEqual([]);

    // Unsafe requests need the double-submit token and an allowed Origin.
    const csrf = (await request.storageState()).cookies.find((c) => c.name === "arranger_csrf")?.value;
    expect(csrf).toBeTruthy();
    const refused = await request.post("/auth/logout", { headers: { Origin: baseURL! } });
    expect(refused.status()).toBe(403);
    const logout = await request.post("/auth/logout", { headers: { Origin: baseURL!, "X-CSRF-Token": csrf! } });
    expect(logout.status()).toBe(200);
    expect((await request.get("/auth/me")).status()).toBe(401);
  });
});
