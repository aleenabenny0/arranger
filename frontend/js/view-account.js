// Account: what is stored, who is signed in, password, export, deletion.

import { api } from "./api.js";
import { h, replace, field, toast, formatBytes, formatDate, confirmDialog, dialog } from "./dom.js";

function meterRow(label, used, limit, format = String) {
  return h("li", {}, h("span", { text: label }),
    h("meter", { min: "0", max: String(limit), high: String(limit * 0.8), value: String(Math.min(used, limit)), "aria-label": label }),
    h("span", { text: `${format(used)} of ${format(limit)}` }));
}

export async function accountView(root, state) {
  const controller = new AbortController();
  const [usage, sessions] = await Promise.all([
    api.get("/account/usage", { signal: controller.signal }), api.get("/auth/sessions", { signal: controller.signal }),
  ]);
  const user = state.user;

  // --- email verification ---
  const verifyBox = h("section", { class: "panel", "aria-labelledby": "verify-h" }, h("h2", { id: "verify-h", text: "Email address" }),
    h("p", { text: `${user.email} · ${user.email_verified ? "verified" : "not verified yet"}` }));
  if (!user.email_verified) {
    const resend = h("button", { type: "button", class: "button", text: "Send the verification link again" });
    resend.addEventListener("click", async () => {
      resend.disabled = true;
      try { await api.post("/auth/email/resend"); toast("Sent. Check your inbox.", "success"); }
      catch (error) { toast(error.message, "error"); } finally { resend.disabled = false; }
    });
    verifyBox.append(resend);
  }

  // --- password ---
  const current = h("input", { type: "password", autocomplete: "current-password", required: true });
  const next = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const currentField = field({ label: "Current password", control: current });
  const nextField = field({ label: "New password", control: next, hint: "At least 10 characters." });
  const changeButton = h("button", { type: "submit", class: "button", text: "Change password" });
  const passwordForm = h("form", { class: "form", novalidate: true, onsubmit: async (event) => {
    event.preventDefault(); currentField.setError(""); nextField.setError("");
    if (!current.value) { currentField.setError("Enter your current password."); current.focus(); return; }
    if (next.value.length < 10) { nextField.setError("Use at least 10 characters."); next.focus(); return; }
    changeButton.disabled = true;
    try {
      await api.post("/auth/password/change", { current_password: current.value, new_password: next.value });
      current.value = ""; next.value = ""; toast("Password changed. Other devices were signed out.", "success");
    } catch (error) { (error.status === 403 || error.status === 401 ? currentField : nextField).setError(error.message); }
    finally { changeButton.disabled = false; }
  } }, currentField, nextField, changeButton);

  // --- sessions ---
  const sessionRows = (sessions.sessions || []).map((s) => h("tr", {},
    h("th", { scope: "row", text: s.current ? "This device" : (s.user_agent || "Unknown device").slice(0, 60) }),
    h("td", { text: s.ip_address || "" }), h("td", { text: formatDate(s.last_seen_at || s.created_at) }),
    h("td", {}, s.current ? null : h("button", { type: "button", class: "button button-small", text: "Sign out", onclick: async (event) => {
      try { await api.del(`/auth/sessions/${s.id}`); event.target.closest("tr").remove(); toast("That device was signed out.", "success"); }
      catch (error) { toast(error.message, "error"); } } }))));

  // --- export and delete ---
  const exportButton = h("button", { type: "button", class: "button", text: "Download a copy of my data" });
  exportButton.addEventListener("click", () => window.location.assign("/account/export"));
  const deleteButton = h("button", { type: "button", class: "button button-danger", text: "Delete my account" });
  deleteButton.addEventListener("click", () => {
    const password = h("input", { type: "password", autocomplete: "current-password", required: true });
    const word = h("input", { type: "text", autocomplete: "off", required: true });
    const passwordField = field({ label: "Your password", control: password });
    const wordField = field({ label: "Type DELETE to confirm", control: word });
    const confirm = h("button", { type: "button", class: "button button-danger", text: "Delete everything" });
    const el = dialog({ title: "Delete your account?", body: h("div", { class: "form" },
      h("p", { text: "Your pieces, arrangements, files and account are removed from this service. This cannot be undone. Download a copy first if you want one." }),
      passwordField, wordField),
      actions: [h("button", { type: "button", class: "button", text: "Keep my account", onclick: () => el.close() }), confirm] });
    confirm.addEventListener("click", async () => {
      passwordField.setError(""); wordField.setError("");
      if (word.value !== "DELETE") { wordField.setError("Type DELETE in capitals."); word.focus(); return; }
      confirm.disabled = true;
      try {
        await api.del("/account", { password: password.value, confirm: "DELETE" });
        el.close(); state.user = null; toast("Your account and data were deleted.", "success"); window.location.hash = "/";
      } catch (error) { passwordField.setError(error.message); } finally { confirm.disabled = false; }
    });
  });

  const signOutAll = h("button", { type: "button", class: "button", text: "Sign out everywhere" });
  signOutAll.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: "Sign out everywhere?", message: "Every device, including this one, will be signed out.", confirmLabel: "Sign out everywhere" }))) return;
    try { await api.post("/auth/logout-all"); state.user = null; window.location.hash = "/login"; } catch (error) { toast(error.message, "error"); }
  });

  replace(root, h("h1", { text: "Account" }), verifyBox,
    h("section", { class: "panel", "aria-labelledby": "usage-h" }, h("h2", { id: "usage-h", text: "What you are storing" }),
      h("ul", { class: "meters" }, meterRow("Pieces", usage.projects.used, usage.projects.limit),
        meterRow("Files", usage.storage_bytes.used, usage.storage_bytes.limit, formatBytes),
        meterRow("Jobs in the last day", usage.jobs_today.used, usage.jobs_today.limit)),
      h("p", { class: "muted", text: `Uploaded originals are kept ${usage.retention.upload_days ? `${usage.retention.upload_days} days` : "until you delete the piece"}. Generated files are kept ${usage.retention.export_days ? `${usage.retention.export_days} days and can be made again` : "until you delete the piece"}.` })),
    h("section", { class: "panel", "aria-labelledby": "pw-h" }, h("h2", { id: "pw-h", text: "Password" }), passwordForm),
    h("section", { class: "panel", "aria-labelledby": "sess-h" }, h("h2", { id: "sess-h", text: "Where you are signed in" }),
      h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Table, scroll sideways if it is cut off" }, h("table", {}, h("thead", {}, h("tr", {}, ["Device", "Network", "Last used", "Sign out"].map((t) => h("th", { scope: "col", text: t })))), h("tbody", {}, sessionRows))), signOutAll),
    h("section", { class: "panel", "aria-labelledby": "data-h" }, h("h2", { id: "data-h", text: "Your data" }),
      h("p", { text: "You can take a copy of everything stored about you, or delete it all." }),
      h("div", { class: "toolbar" }, exportButton, deleteButton),
      h("p", { class: "muted" }, h("a", { href: "privacy.html", text: "Privacy notice" }))));
  return () => controller.abort();
}
