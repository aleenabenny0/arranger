import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { apiError, installApi, jsonResponse, type FakeApi } from "../test/fakeApi";
import { CATALOG, renderView, USER } from "../test/render";
import { Account, retentionText } from "./Account";
import { Auth } from "./Auth";
import { Home } from "./Home";
import { Library, rowMeta, summaryText } from "./Library";
import { Forgot, Reset, VerifyEmail } from "./Password";

let api: FakeApi;

beforeEach(() => {
  api = installApi();
  window.location.hash = "";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Home", () => {
  it("tells a visitor what the server can do and where to start", () => {
    renderView(<Home />, { user: null });
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Piano arrangements that fit your hands");
    expect(screen.getByTestId("home-register")).toHaveAttribute("href", "#/register");
    expect(screen.getByText("Audio transcription is not installed on this server.")).toBeInTheDocument();
    expect(screen.getByText(/PDF engraving is not installed/)).toBeInTheDocument();
  });

  it("sends a signed-in user to their pieces", () => {
    renderView(<Home />, { catalog: { ...CATALOG, capabilities: { export_pdf: { available: true }, import_audio: { available: true } } } });
    expect(screen.getByRole("link", { name: "Go to your pieces" })).toHaveAttribute("href", "#/library");
    expect(screen.getByText(/Audio recordings/)).toBeInTheDocument();
    expect(screen.getByText("Downloads: MIDI, MusicXML and a printable PDF")).toBeInTheDocument();
  });
});

describe("Auth", () => {
  it("refuses an invalid email and a short password before any request", async () => {
    const user = userEvent.setup();
    renderView(<Auth mode="register" />, { user: null });
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Create your account");
    await user.type(screen.getByTestId("auth-email"), "not-an-email");
    await user.click(screen.getByTestId("auth-submit"));
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    await user.clear(screen.getByTestId("auth-email"));
    await user.type(screen.getByTestId("auth-email"), "a@b.co");
    await user.type(screen.getByTestId("auth-password"), "short");
    await user.click(screen.getByTestId("auth-submit"));
    expect(screen.getByText("Use at least 10 characters.")).toBeInTheDocument();
    expect(api.calls).toHaveLength(0);
  });

  it("signs in, stores the user and goes to the library", async () => {
    const user = userEvent.setup();
    api.on("POST", "/auth/login", { user: USER });
    renderView(<Auth mode="login" />, { user: null });
    await user.type(screen.getByTestId("auth-email"), " pianist@example.com ");
    await user.type(screen.getByTestId("auth-password"), "correct-horse-battery-7");
    await user.click(screen.getByTestId("auth-submit"));
    await waitFor(() => expect(window.location.hash).toBe("#/library"));
    expect(api.sent("POST", "/auth/login")[0].body).toEqual({ email: "pianist@example.com", password: "correct-horse-battery-7" });
    expect(screen.getByTestId("toast-success")).toHaveTextContent("Signed in.");
  });

  it("shows a wrong password in words and stays on the form", async () => {
    const user = userEvent.setup();
    api.on("POST", "/auth/login", apiError(401, "Wrong email or password.", "bad_credentials"));
    renderView(<Auth mode="login" />, { user: null });
    await user.type(screen.getByTestId("auth-email"), "pianist@example.com");
    await user.type(screen.getByTestId("auth-password"), "definitely-not-the-password");
    await user.click(screen.getByTestId("auth-submit"));
    await waitFor(() => expect(screen.getByTestId("auth-error")).toBeVisible());
    expect(screen.getByTestId("auth-error")).toHaveTextContent(/^Wrong email or password\./);
    expect(window.location.hash).toBe("");
    expect(screen.getByTestId("auth-submit")).toBeEnabled();
  });

  it("registers with an optional name", async () => {
    const user = userEvent.setup();
    api.on("POST", "/auth/register", { user: { ...USER, email_verified: false } });
    renderView(<Auth mode="register" />, { user: null });
    await user.type(screen.getByTestId("auth-email"), "new@example.com");
    await user.type(screen.getByLabelText("Name (optional)"), "New");
    await user.type(screen.getByTestId("auth-password"), "correct-horse-battery-7");
    await user.click(screen.getByTestId("auth-submit"));
    await waitFor(() => expect(api.sent("POST", "/auth/register")).toHaveLength(1));
    expect(api.sent("POST", "/auth/register")[0].body).toEqual({ email: "new@example.com", password: "correct-horse-battery-7", display_name: "New" });
    await waitFor(() => expect(screen.getByTestId("toast-success")).toHaveTextContent("Account created."));
  });
});

describe("password flows", () => {
  it("asks for a reset link without saying whether the address exists", async () => {
    const user = userEvent.setup();
    api.on("POST", "/auth/password-reset/request", {});
    renderView(<Forgot />, { user: null });
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Email address"), "pianist@example.com");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    await waitFor(() => expect(screen.getByText(/a reset link is on its way/)).toBeInTheDocument());
  });

  it("sets a new password from a token and scrubs it from the address", async () => {
    const user = userEvent.setup();
    window.location.hash = "/reset-password?token=t1";
    api.on("POST", "/auth/password-reset/confirm", { user: USER });
    renderView(<Reset token="t1" />, { user: null });
    expect(window.location.hash).toBe("#/reset-password");
    await user.type(screen.getByLabelText("New password"), "correct-horse-battery-7");
    await user.type(screen.getByLabelText("New password again"), "different-password-9");
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    expect(screen.getByText("The two passwords do not match.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("New password again"));
    await user.type(screen.getByLabelText("New password again"), "correct-horse-battery-7");
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    await waitFor(() => expect(window.location.hash).toBe("#/library"));
    expect(api.sent("POST", "/auth/password-reset/confirm")[0].body).toEqual({ token: "t1", password: "correct-horse-battery-7" });
  });

  it("explains an incomplete reset link", () => {
    renderView(<Reset token={null} />, { user: null });
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("This link is incomplete");
  });

  it("verifies an email token, and says when it did not work", async () => {
    api.on("POST", "/auth/email/verify", (call) => ((call.body as { token: string }).token === "good" ? jsonResponse(200, { user: USER }) : apiError(400, "That link has expired.")));
    const first = renderView(<VerifyEmail token="good" />, { user: null });
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Email verified"));
    expect(screen.getByRole("link", { name: "Go to your pieces" })).toBeInTheDocument();
    first.unmount();
    renderView(<VerifyEmail token="bad" />, { user: null });
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("That link did not work"));
    expect(screen.getByText(/That link has expired/)).toBeInTheDocument();
  });
});

const PROJECT = { id: "p1", title: "Waltz", composer: "Anon", source_kind: "midi", bar_count: 16, arrangement_count: 2, updated_at: "2026-09-29T10:00:00Z" };

describe("Library", () => {
  it("lists pieces with their facts and pages through them", async () => {
    api.on("GET", "/projects", (_call, url) => jsonResponse(200, { projects: url.searchParams.get("offset") === "0" ? [PROJECT] : [], total: 13, limit: 12, offset: Number(url.searchParams.get("offset")) }));
    const user = userEvent.setup();
    renderView(<Library />);
    await waitFor(() => expect(screen.getByTestId("library-summary")).toHaveTextContent("Showing 1 to 1 of 13."));
    expect(screen.getByTestId("project-link")).toHaveAttribute("href", "#/project/p1");
    expect(screen.getByText(rowMeta(PROJECT))).toBeInTheDocument();
    expect(rowMeta(PROJECT)).toMatch(/^MIDI · Anon · 16 bars · 2 arrangements · changed /);
    await user.click(screen.getByRole("button", { name: "Next" }));
    // The second page is empty, so the view steps back to the first.
    await waitFor(() => expect(api.sent("GET", "/projects").length).toBeGreaterThanOrEqual(3));
    expect(summaryText({ projects: [], total: 0, limit: 12, offset: 0 }, 0, "")).toBe("No pieces yet. Upload one to begin.");
    expect(summaryText({ projects: [], total: 0, limit: 12, offset: 0 }, 0, "nothing")).toBe("Nothing matches “nothing”.");
  });

  it("uploads a file and opens the new piece", async () => {
    api.on("GET", "/projects", { projects: [], total: 0, limit: 12, offset: 0 });
    api.on("POST", "/projects/import", jsonResponse(201, { project: { id: "p9", title: "tune.mid" } }));
    const user = userEvent.setup();
    renderView(<Library />);
    await waitFor(() => expect(screen.getByTestId("library-summary")).toHaveTextContent("No pieces yet. Upload one to begin."));
    await user.upload(screen.getByTestId("upload-input"), new File(["MThd"], "tune.mid", { type: "audio/midi" }));
    await waitFor(() => expect(window.location.hash).toBe("#/project/p9"));
    expect(api.sent("POST", "/projects/import")[0].headers.get("idempotency-key")).toMatch(/^[0-9a-f]{32}$/);
    expect(screen.getByTestId("toast-success")).toHaveTextContent("Imported “tune.mid”.");
  });

  it("explains a refused upload in words and leaves the list alone", async () => {
    api.on("GET", "/projects", { projects: [], total: 0, limit: 12, offset: 0 });
    api.on("POST", "/projects/import", apiError(400, "That is not a MIDI file.", "invalid_file"));
    const user = userEvent.setup();
    renderView(<Library />);
    await waitFor(() => expect(screen.getByTestId("library-summary")).toHaveTextContent("No pieces yet."));
    await user.upload(screen.getByTestId("upload-input"), new File(["junk"], "notes.mid"));
    await waitFor(() => expect(screen.getByTestId("upload-error")).toBeVisible());
    expect(screen.getByTestId("upload-error")).toHaveTextContent(/^That is not a MIDI file\./);
    expect(screen.getByTestId("library-summary")).toHaveTextContent("No pieces yet.");
    expect(window.location.hash).toBe("");
  });

  it("refuses an empty file without a request", async () => {
    api.on("GET", "/projects", { projects: [], total: 0, limit: 12, offset: 0 });
    const user = userEvent.setup();
    renderView(<Library />);
    await user.upload(screen.getByTestId("upload-input"), new File([], "empty.mid"));
    await waitFor(() => expect(screen.getByTestId("upload-error")).toHaveTextContent("That file is empty."));
    expect(api.sent("POST", "/projects/import")).toHaveLength(0);
  });

  it("renames and deletes a piece through dialogs", async () => {
    api.on("GET", "/projects", { projects: [PROJECT], total: 1, limit: 12, offset: 0 });
    api.on("PATCH", "/projects/p1", { project: { ...PROJECT, title: "Valse" } });
    api.on("DELETE", "/projects/p1", {});
    const user = userEvent.setup();
    renderView(<Library />);
    await waitFor(() => expect(screen.getByTestId("project-link")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Rename Waltz" }));
    const dialog = screen.getByRole("dialog", { name: "Rename piece" });
    await user.clear(within(dialog).getByLabelText("Title"));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(within(dialog).getByText("A piece needs a title.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Title"), "Valse");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.sent("PATCH", "/projects/p1")[0].body).toEqual({ title: "Valse", composer: "Anon" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.click(screen.getByRole("button", { name: "Delete Waltz" }));
    await user.click(within(screen.getByRole("dialog", { name: "Delete this piece?" })).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.sent("DELETE", "/projects/p1")).toHaveLength(1));
    expect(screen.getByText("Done: Deleted “Waltz”.")).toBeInTheDocument();
  });

  it("reminds an unverified user to verify first", async () => {
    api.on("GET", "/projects", { projects: [], total: 0, limit: 12, offset: 0 });
    renderView(<Library />, { user: { ...USER, email_verified: false } });
    expect(screen.getByRole("note")).toHaveTextContent("Verify your email address to upload and arrange.");
  });
});

const USAGE = { projects: { used: 2, limit: 50 }, storage_bytes: { used: 1024 * 1024, limit: 200 * 1024 * 1024 }, jobs_today: { used: 1, limit: 100 }, retention: { upload_days: 0, export_days: 30 } };
const SESSIONS = { sessions: [
  { id: "s1", current: true, user_agent: "Chrome", ip: "203.0.113.0/24", created_at: "2026-09-29T09:00:00Z", last_seen_at: "2026-09-29T10:00:00Z" },
  { id: "s2", current: false, user_agent: "Firefox", ip: "", created_at: "2026-09-28T09:00:00Z", last_seen_at: null },
] };

describe("Account", () => {
  it("shows storage, sessions and retention, and signs out another device", async () => {
    api.on("GET", "/account/usage", USAGE).on("GET", "/auth/sessions", SESSIONS).on("DELETE", "/auth/sessions/s2", {});
    const user = userEvent.setup();
    renderView(<Account />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Account"));
    await waitFor(() => expect(screen.getByText("pianist@example.com · verified")).toBeInTheDocument());
    expect(screen.getByText("2 of 50")).toBeInTheDocument();
    expect(screen.getByText("1.0 MB of 200.0 MB")).toBeInTheDocument();
    expect(screen.getByText(retentionText(USAGE.retention))).toHaveTextContent("kept until you delete the piece. Generated files are kept 30 days");
    expect(screen.getByRole("rowheader", { name: "This device" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(api.sent("DELETE", "/auth/sessions/s2")).toHaveLength(1));
    expect(screen.queryByRole("rowheader", { name: "Firefox" })).toBeNull();
  });

  it("puts a password error next to the right field", async () => {
    api.on("GET", "/account/usage", USAGE).on("GET", "/auth/sessions", { sessions: [] });
    api.on("POST", "/auth/password/change", apiError(403, "That is not your current password.", "bad_credentials"));
    const user = userEvent.setup();
    renderView(<Account />);
    await waitFor(() => expect(screen.getByLabelText("Current password")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByText("Enter your current password.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Current password"), "old-password-here");
    await user.type(screen.getByLabelText("New password"), "new-password-here-2");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(screen.getByLabelText("Current password")).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByText(/That is not your current password/)).toBeInTheDocument();
  });

  it("offers to resend the verification link when the address is not verified", async () => {
    api.on("GET", "/account/usage", USAGE).on("GET", "/auth/sessions", { sessions: [] }).on("POST", "/auth/email/resend", {});
    const user = userEvent.setup();
    renderView(<Account />, { user: { ...USER, email_verified: false } });
    await waitFor(() => expect(screen.getByText("pianist@example.com · not verified yet")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Send the verification link again" }));
    await waitFor(() => expect(screen.getByTestId("toast-success")).toHaveTextContent("Sent. Check your inbox."));
  });

  it("deletes the account only with the password and the word DELETE", async () => {
    api.on("GET", "/account/usage", USAGE).on("GET", "/auth/sessions", { sessions: [] }).on("DELETE", "/account", {});
    const user = userEvent.setup();
    renderView(<Account />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Delete my account" })).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Delete my account" }));
    const dialog = screen.getByRole("dialog", { name: "Delete your account?" });
    await user.type(within(dialog).getByLabelText("Your password"), "correct-horse-battery-7");
    await user.type(within(dialog).getByLabelText("Type DELETE to confirm"), "delete");
    await user.click(within(dialog).getByRole("button", { name: "Delete everything" }));
    expect(within(dialog).getByText("Type DELETE in capitals.")).toBeInTheDocument();
    expect(api.sent("DELETE", "/account")).toHaveLength(0);
    await user.clear(within(dialog).getByLabelText("Type DELETE to confirm"));
    await user.type(within(dialog).getByLabelText("Type DELETE to confirm"), "DELETE");
    await user.click(within(dialog).getByRole("button", { name: "Delete everything" }));
    await waitFor(() => expect(api.sent("DELETE", "/account")[0].body).toEqual({ password: "correct-horse-battery-7", confirm: "DELETE" }));
    await waitFor(() => expect(window.location.hash).toBe("#/"));
  });

  it("shows a load failure in words", async () => {
    api.on("GET", "/account/usage", apiError(500, "The database is unavailable.")).on("GET", "/auth/sessions", { sessions: [] });
    renderView(<Account />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Something went wrong"));
    expect(screen.getByText(/database is unavailable/)).toBeInTheDocument();
  });
});

describe("App", () => {
  it("shows the signed-out navigation and sends protected routes to sign in", async () => {
    window.location.hash = "/library";
    renderView(<App />, { user: null });
    await waitFor(() => expect(window.location.hash).toBe("#/login"));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Sign in"));
    expect(screen.getByTestId("nav-login")).toHaveAttribute("aria-current", "page");
    expect(screen.queryByTestId("nav-sign-out")).toBeNull();
  });

  it("signs out from the navigation", async () => {
    api.on("POST", "/auth/logout", {}).on("GET", "/projects", { projects: [], total: 0, limit: 12, offset: 0 });
    window.location.hash = "/library";
    const user = userEvent.setup();
    renderView(<App />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Your pieces"));
    expect(screen.getByTestId("nav-library")).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByTestId("nav-sign-out"));
    await waitFor(() => expect(window.location.hash).toBe("#/"));
    await waitFor(() => expect(screen.getByTestId("nav-login")).toBeInTheDocument());
    expect(screen.getByTestId("toast-success")).toHaveTextContent("Signed out.");
    expect(api.sent("POST", "/auth/logout")).toHaveLength(1);
  });

  it("moves focus to the content from the skip link", async () => {
    const user = userEvent.setup();
    renderView(<App />, { user: null });
    await user.click(screen.getByText("Skip to content"));
    expect(document.activeElement?.id).toBe("main");
  });
});
