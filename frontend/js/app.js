// Application shell: session, routing, navigation, and the signed-out views.
//
// Routing is hash-based so the app is one static file on any host. Tokens in
// emailed links live in the fragment (#/reset-password?token=...), which the
// browser never sends to a server, and are removed from the address bar as
// soon as they are read.

import { api, ApiError } from "./api.js";
import { h, replace, field, toast, announce } from "./dom.js";
import { libraryView } from "./view-library.js";
import { projectView } from "./view-project.js";
import { accountView } from "./view-account.js";

export const state = { user: null, catalog: null, teardown: null };

const main = () => document.getElementById("main");

function parseRoute() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const [path, queryString] = hash.split("?");
  const parts = path.split("/").filter(Boolean);
  return { parts, query: new URLSearchParams(queryString || "") };
}

export function go(path) {
  if (window.location.hash === `#${path}`) render(); else window.location.hash = path;
}

function setTitle(text) {
  document.title = `${text} · Arranger`;
}

// Move focus to the new view's heading so keyboard and screen-reader users land
// where the content changed, the way a page load would. Not on the first render:
// a real page load starts at the top, where the skip link is the first Tab stop.
let firstRender = true;
function focusHeading() {
  if (firstRender) { firstRender = false; return; }
  const heading = main().querySelector("h1");
  if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: false }); }
}

function renderNav() {
  const nav = document.getElementById("nav");
  const current = parseRoute().parts[0] || "";
  const link = (href, text, key) => h("a", { href, text, "aria-current": current === key ? "page" : null });
  if (state.user) {
    replace(nav,
      link("#/library", "Your pieces", "library"),
      link("#/account", "Account", "account"),
      h("button", { type: "button", class: "link-button", text: "Sign out", onclick: signOut }));
  } else {
    replace(nav, link("#/", "About", ""), link("#/login", "Sign in", "login"), link("#/register", "Create account", "register"));
  }
}

async function signOut() {
  try { await api.post("/auth/logout"); } catch { /* the session may already be gone */ }
  state.user = null;
  toast("Signed out.", "success");
  go("/");
}

export async function refreshUser() {
  try { state.user = (await api.get("/auth/me")).user; } catch { state.user = null; }
  return state.user;
}

async function render() {
  if (state.teardown) { try { state.teardown(); } catch { /* view already gone */ } state.teardown = null; }
  const { parts, query } = parseRoute();
  const [section, id] = parts;
  renderNav();
  const needsUser = ["library", "project", "account"].includes(section);
  if (needsUser && !state.user) { go("/login"); return; }
  const root = main();
  try {
    if (section === "library") { setTitle("Your pieces"); state.teardown = await libraryView(root, state); }
    else if (section === "project" && id) { setTitle("Piece"); state.teardown = await projectView(root, state, id, setTitle); }
    else if (section === "account") { setTitle("Account"); state.teardown = await accountView(root, state); }
    else if (section === "login") { setTitle("Sign in"); authView(root, "login"); }
    else if (section === "register") { setTitle("Create account"); authView(root, "register"); }
    else if (section === "forgot") { setTitle("Reset password"); forgotView(root); }
    else if (section === "reset-password") { setTitle("Choose a new password"); resetView(root, query.get("token")); }
    else if (section === "verify-email") { setTitle("Verify email"); await verifyView(root, query.get("token")); }
    else { setTitle("Piano arrangements for your hands"); homeView(root); }
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) { state.user = null; go("/login"); return; }
    replace(root, h("h1", { text: "Something went wrong" }), h("p", { role: "alert", text: error.message }),
      h("p", {}, h("a", { href: "#/library", text: "Back to your pieces" })));
  }
  focusHeading();
}

// --- signed-out views -------------------------------------------------------------

function homeView(root) {
  const caps = (state.catalog && state.catalog.capabilities) || {};
  const pdf = caps.export_pdf && caps.export_pdf.available;
  const audio = caps.import_audio && caps.import_audio.available;
  replace(root,
    h("section", { class: "hero" },
      h("h1", { text: "Piano arrangements that fit your hands" }),
      h("p", { class: "lead", text: "Upload a piece, tell Arranger what your hands can do, and get a two-hand version you can read, hear and print." }),
      h("p", {}, state.user
        ? h("a", { class: "button button-primary", href: "#/library", text: "Go to your pieces" })
        : h("a", { class: "button button-primary", href: "#/register", text: "Create a free account" }))),
    h("section", { class: "cards" },
      card("What you can upload", [
        "MIDI files (.mid)", "MusicXML files (.musicxml, .xml, .mxl)",
        audio ? "Audio recordings (WAV, FLAC, OGG, MP3), transcribed by a model. Expect to correct wrong notes first."
          : "Audio transcription is not installed on this server.",
      ]),
      card("What you get", [
        "A melody-first reduction for two hands, made for your reach and speed",
        "Notation in your browser, with anything outside your limits marked",
        "Playback, with the original to compare against",
        pdf ? "Downloads: MIDI, MusicXML and a printable PDF" : "Downloads: MIDI and MusicXML (PDF engraving is not installed on this server)",
      ]),
      card("What it does not do", [
        "It does not guarantee a piece is comfortable. It checks reach, finger count and how fast your hands must move, as a model understands them.",
        "It does not work out fingering in detail.",
        "It keeps the tune and simplifies the rest. It does not compose.",
      ])),
    h("p", { class: "muted", text: "Only upload music you have the right to use. " }, h("a", { href: "copyright.html", text: "Upload rights" })));
}

function card(title, items) {
  return h("article", { class: "card" }, h("h2", { text: title }), h("ul", {}, items.map((text) => h("li", { text }))));
}

function authView(root, mode) {
  const isRegister = mode === "register";
  const email = h("input", { type: "email", name: "email", autocomplete: "email", required: true, inputmode: "email" });
  const password = h("input", { type: "password", name: "password", required: true, minlength: isRegister ? 10 : 1,
    autocomplete: isRegister ? "new-password" : "current-password" });
  const name = h("input", { type: "text", name: "display_name", autocomplete: "name", maxlength: 80 });
  const emailField = field({ label: "Email address", control: email });
  const passwordField = field({ label: "Password", control: password,
    hint: isRegister ? "At least 10 characters. A few unrelated words works well." : "" });
  const submit = h("button", { type: "submit", class: "button button-primary", text: isRegister ? "Create account" : "Sign in" });
  const formError = h("p", { class: "form-error", role: "alert" });
  formError.hidden = true;

  const form = h("form", { class: "form", novalidate: true, onsubmit: async (event) => {
    event.preventDefault();
    emailField.setError(""); passwordField.setError(""); formError.hidden = true;
    if (!email.validity.valid) { emailField.setError("Enter a valid email address."); email.focus(); return; }
    if (!password.value || (isRegister && password.value.length < 10)) {
      passwordField.setError(isRegister ? "Use at least 10 characters." : "Enter your password."); password.focus(); return;
    }
    submit.disabled = true;
    try {
      const body = { email: email.value.trim(), password: password.value };
      if (isRegister) body.display_name = name.value.trim();
      state.user = (await api.post(isRegister ? "/auth/register" : "/auth/login", body)).user;
      password.value = "";
      toast(isRegister ? "Account created. Check your email to verify your address." : "Signed in.", "success");
      go("/library");
    } catch (error) {
      formError.textContent = error.message; formError.hidden = false; formError.focus?.();
      announce(error.message, { urgent: true });
    } finally { submit.disabled = false; }
  } },
    emailField, isRegister ? field({ label: "Name (optional)", control: name }) : null, passwordField, formError, submit);

  replace(root, h("div", { class: "narrow" },
    h("h1", { text: isRegister ? "Create your account" : "Sign in" }), form,
    h("p", {}, isRegister
      ? ["Already have an account? ", h("a", { href: "#/login", text: "Sign in" })]
      : [h("a", { href: "#/forgot", text: "Forgot your password?" }), " · ", h("a", { href: "#/register", text: "Create an account" })]),
    isRegister ? h("p", { class: "muted" }, "By creating an account you agree to the ", h("a", { href: "terms.html", text: "terms" }),
      " and have read the ", h("a", { href: "privacy.html", text: "privacy notice" }), ".") : null));
}

function forgotView(root) {
  const email = h("input", { type: "email", autocomplete: "email", required: true });
  const emailField = field({ label: "Email address", control: email });
  const done = h("p", { role: "status" });
  const submit = h("button", { type: "submit", class: "button button-primary", text: "Send reset link" });
  replace(root, h("div", { class: "narrow" }, h("h1", { text: "Reset your password" }),
    h("form", { class: "form", novalidate: true, onsubmit: async (event) => {
      event.preventDefault();
      if (!email.validity.valid) { emailField.setError("Enter a valid email address."); email.focus(); return; }
      emailField.setError(""); submit.disabled = true;
      try {
        await api.post("/auth/password-reset/request", { email: email.value.trim() });
        done.textContent = "If that address has an account, a reset link is on its way. It expires soon.";
      } catch (error) { done.textContent = error.message; } finally { submit.disabled = false; }
    } }, emailField, submit), done, h("p", {}, h("a", { href: "#/login", text: "Back to sign in" }))));
}

function scrubTokenFromAddressBar(path) {
  window.history.replaceState(null, "", `${window.location.pathname}#${path}`);
}

function resetView(root, token) {
  scrubTokenFromAddressBar("/reset-password");
  if (!token) {
    replace(root, h("div", { class: "narrow" }, h("h1", { text: "This link is incomplete" }),
      h("p", { text: "Open the link from your email again, or request a new one." }), h("a", { href: "#/forgot", text: "Request a new link" })));
    return;
  }
  const password = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const again = h("input", { type: "password", autocomplete: "new-password", required: true });
  const passwordField = field({ label: "New password", control: password, hint: "At least 10 characters." });
  const againField = field({ label: "New password again", control: again });
  const formError = h("p", { class: "form-error", role: "alert" }); formError.hidden = true;
  const submit = h("button", { type: "submit", class: "button button-primary", text: "Set new password" });
  replace(root, h("div", { class: "narrow" }, h("h1", { text: "Choose a new password" }),
    h("form", { class: "form", novalidate: true, onsubmit: async (event) => {
      event.preventDefault();
      passwordField.setError(""); againField.setError(""); formError.hidden = true;
      if (password.value.length < 10) { passwordField.setError("Use at least 10 characters."); password.focus(); return; }
      if (password.value !== again.value) { againField.setError("The two passwords do not match."); again.focus(); return; }
      submit.disabled = true;
      try {
        state.user = (await api.post("/auth/password-reset/confirm", { token, password: password.value })).user;
        password.value = ""; again.value = "";
        toast("Password changed. Other devices were signed out.", "success");
        go("/library");
      } catch (error) { formError.textContent = error.message; formError.hidden = false; } finally { submit.disabled = false; }
    } }, passwordField, againField, formError, submit)));
}

async function verifyView(root, token) {
  scrubTokenFromAddressBar("/verify-email");
  replace(root, h("div", { class: "narrow" }, h("h1", { text: "Verifying your email" }), h("p", { role: "status", text: "One moment…" })));
  if (!token) { replace(root, h("div", { class: "narrow" }, h("h1", { text: "This link is incomplete" }), h("p", { text: "Open the link from your email again." }))); return; }
  try {
    state.user = (await api.post("/auth/email/verify", { token })).user;
    replace(root, h("div", { class: "narrow" }, h("h1", { text: "Email verified" }), h("p", { text: "Thank you. You can use every feature now." }),
      h("a", { class: "button button-primary", href: "#/library", text: "Go to your pieces" })));
  } catch (error) {
    replace(root, h("div", { class: "narrow" }, h("h1", { text: "That link did not work" }), h("p", { role: "alert", text: error.message }),
      h("p", { text: "Links expire and work once. Sign in and ask for a new one from your account page." })));
  }
}

// --- start ---------------------------------------------------------------------------

async function start() {
  // The address fragment is the route here, so the skip link cannot be a plain
  // "#main" jump: it would navigate. Move focus instead, which is what it is for.
  const skip = document.querySelector(".skip-link");
  if (skip) skip.addEventListener("click", (event) => { event.preventDefault(); main().focus(); main().scrollIntoView(); });
  try { state.catalog = await api.get("/catalog"); } catch { state.catalog = null; }
  await refreshUser();
  window.addEventListener("hashchange", render);
  if (!window.location.hash) window.location.hash = state.user ? "/library" : "/";
  else render();
}

start();
