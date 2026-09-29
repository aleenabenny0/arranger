// DOM helpers. Nothing in this app assigns innerHTML: every piece of text that
// came from a user or a server reaches the page through textContent or an
// attribute set by name, so a title like <img onerror=...> is only ever text.

const BOOLEAN_PROPS = new Set(["disabled", "checked", "hidden", "selected", "required", "open", "multiple"]);

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (BOOLEAN_PROPS.has(key)) el[key] = Boolean(value);
    else if (key === "value") el.value = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replace(el, ...children) {
  return append(clear(el), children);
}

let idCounter = 0;
export function uid(prefix = "id") {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

// A labelled form control with room for a hint and an error, all associated
// so a screen reader hears them with the field.
export function field({ label, hint, control, error }) {
  const id = control.id || uid("f");
  control.id = id;
  const described = [];
  const parts = [h("label", { for: id, text: label })];
  if (hint) {
    const hintEl = h("p", { class: "hint", id: `${id}-hint`, text: hint });
    described.push(hintEl.id);
    parts.push(hintEl);
  }
  parts.push(control);
  const errorEl = h("p", { class: "field-error", id: `${id}-error`, role: "alert" });
  errorEl.hidden = true;
  described.push(errorEl.id);
  parts.push(errorEl);
  control.setAttribute("aria-describedby", described.join(" "));
  const wrapper = h("div", { class: "field" }, parts);
  wrapper.setError = (message) => {
    errorEl.textContent = message || "";
    errorEl.hidden = !message;
    if (message) control.setAttribute("aria-invalid", "true");
    else control.removeAttribute("aria-invalid");
  };
  if (error) wrapper.setError(error);
  return wrapper;
}

// Status messages. Polite for progress, assertive for errors. The visible
// toast and the live region are separate so re-rendering one never re-announces.
const politeRegion = () => document.getElementById("live-polite");
const assertiveRegion = () => document.getElementById("live-assertive");

export function announce(message, { urgent = false } = {}) {
  const region = urgent ? assertiveRegion() : politeRegion();
  if (!region) return;
  region.textContent = "";
  window.setTimeout(() => { region.textContent = message; }, 30);
}

export function toast(message, kind = "info") {
  const host = document.getElementById("toasts");
  if (!host) return;
  const label = kind === "error" ? "Error: " : kind === "success" ? "Done: " : "";
  const item = h("div", { class: `toast toast-${kind}`, "data-testid": `toast-${kind}` },
    h("span", { class: "toast-icon", "aria-hidden": "true", text: kind === "error" ? "!" : kind === "success" ? "✓" : "i" }),
    h("span", { text: label + message }),
    h("button", { type: "button", class: "icon-button", "aria-label": "Dismiss message", onclick: () => item.remove(), text: "×" }));
  while (host.children.length >= 3) host.firstChild.remove();   // never bury the page under messages
  host.append(item);
  announce(label + message, { urgent: kind === "error" });
  window.setTimeout(() => item.remove(), kind === "error" ? 12000 : 6000);
}

export function formatDate(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function formatClock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const NOTE_NAMES = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"];
export function pitchName(midi) {
  return `${NOTE_NAMES[((midi % 12) + 12) % 12]}${Math.floor(midi / 12) - 1}`;
}

// Tabs following the WAI-ARIA pattern: roving tabindex, arrow keys, Home/End.
export function tabs({ label, items, onChange, initial }) {
  const list = h("div", { role: "tablist", "aria-label": label, class: "tablist" });
  const panels = h("div", { class: "tabpanels" });
  const buttons = [];
  let active = initial || items[0].id;

  function select(id, focus) {
    active = id;
    for (const button of buttons) {
      const on = button.dataset.tab === id;
      button.setAttribute("aria-selected", String(on));
      button.tabIndex = on ? 0 : -1;
      if (on && focus) button.focus();
    }
    for (const panel of panels.children) panel.hidden = panel.dataset.tab !== id;
    if (onChange) onChange(id);
  }

  items.forEach((item, index) => {
    const tabId = uid("tab");
    const panelId = uid("panel");
    const button = h("button", {
      type: "button", role: "tab", id: tabId, "aria-controls": panelId, class: "tab", dataset: { tab: item.id },
      "data-testid": `tab-${item.id}`,
      text: item.label, onclick: () => select(item.id, false),
      onkeydown: (event) => {
        const keys = { ArrowRight: 1, ArrowLeft: -1 };
        let next = null;
        if (event.key in keys) next = (index + keys[event.key] + items.length) % items.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = items.length - 1;
        if (next !== null) { event.preventDefault(); select(items[next].id, true); }
      },
    });
    buttons.push(button);
    list.append(button);
    panels.append(h("div", { role: "tabpanel", id: panelId, "aria-labelledby": tabId, tabindex: "0", class: "tabpanel", dataset: { tab: item.id },
      "data-testid": `panel-${item.id}` }, item.content));
  });
  select(active, false);
  const root = h("div", { class: "tabs" }, list, panels);
  root.select = (id) => select(id, false);
  return root;
}

// A modal built on <dialog>: the browser traps focus and handles Escape.
export function dialog({ title, body, actions }) {
  const titleId = uid("dlg");
  const el = h("dialog", { "aria-labelledby": titleId, class: "dialog", "data-testid": "dialog" },
    h("h2", { id: titleId, text: title }), body, h("div", { class: "dialog-actions" }, actions));
  el.addEventListener("close", () => el.remove());
  document.body.append(el);
  el.showModal();
  return el;
}

export function confirmDialog({ title, message, confirmLabel, danger = false }) {
  return new Promise((resolve) => {
    const el = dialog({
      title, body: h("p", { text: message }),
      actions: [
        h("button", { type: "button", class: "button", text: "Cancel", onclick: () => { el.close(); resolve(false); } }),
        h("button", { type: "button", class: danger ? "button button-danger" : "button button-primary", text: confirmLabel,
          onclick: () => { el.close(); resolve(true); } }),
      ],
    });
    el.addEventListener("cancel", () => resolve(false));
  });
}
