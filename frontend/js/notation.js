// Notation preview: Verovio renders the same MusicXML the user downloads.
//
// The engine is 7 MB of WebAssembly, so it is loaded the first time a score is
// shown, not at page load. Its SVG is parsed as a document and adopted node by
// node after scripts, foreignObject and event-handler attributes are removed;
// innerHTML is never used. Note ids in the MusicXML survive into the SVG, which
// is what lets a finding point at its exact notes.

import { h, replace, announce } from "./dom.js";

const VEROVIO_VERSION = "6.3.0";   // keep in step with fetch_vendor.py

let enginePromise = null;

function loadEngine() {
  if (enginePromise) return enginePromise;
  enginePromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `vendor/verovio-toolkit-wasm.js?v=${VEROVIO_VERSION}`;   // versioned: the server lets it be cached for a week
    script.onerror = () => { enginePromise = null; reject(new Error("The notation engine could not be loaded. Run fetch_vendor.py on the server.")); };
    script.onload = () => {
      const ready = () => resolve(new window.verovio.toolkit());
      if (window.verovio && window.verovio.module && window.verovio.module.calledRun) ready();
      else if (window.verovio && window.verovio.module) window.verovio.module.onRuntimeInitialized = ready;
      else reject(new Error("The notation engine did not start."));
    };
    document.head.append(script);
  });
  return enginePromise;
}

function sanitise(svgText) {
  const parsed = new DOMParser().parseFromString(svgText, "image/svg+xml");
  if (parsed.querySelector("parsererror")) throw new Error("The score could not be drawn.");
  for (const el of parsed.querySelectorAll("script, foreignObject, iframe, object, embed")) el.remove();
  for (const el of parsed.querySelectorAll("*")) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on") || ((name === "href" || name === "xlink:href") && !attr.value.startsWith("#"))) el.removeAttribute(attr.name);
    }
  }
  return document.importNode(parsed.documentElement, true);
}

export class NotationView {
  constructor({ label }) {
    this.page = 1;
    this.pages = 1;
    this.zoom = 40;
    this.findings = [];
    this.toolkit = null;
    this.status = h("p", { class: "muted", role: "status", text: "" });
    this.canvas = h("div", { class: "notation-canvas", role: "img", "aria-label": label, tabindex: "0" });
    this.pageLabel = h("span", { class: "page-label", "aria-live": "polite" });
    this.prev = h("button", { type: "button", class: "button", text: "Previous page", onclick: () => this.show(this.page - 1) });
    this.next = h("button", { type: "button", class: "button", text: "Next page", onclick: () => this.show(this.page + 1) });
    const zoomOut = h("button", { type: "button", class: "button", "aria-label": "Smaller notes", text: "A−", onclick: () => this.setZoom(this.zoom - 8) });
    const zoomIn = h("button", { type: "button", class: "button", "aria-label": "Larger notes", text: "A+", onclick: () => this.setZoom(this.zoom + 8) });
    this.root = h("section", { class: "notation" },
      h("div", { class: "toolbar", role: "group", "aria-label": "Score pages" }, this.prev, this.pageLabel, this.next, zoomOut, zoomIn),
      this.status, this.canvas);
    this._resize = () => { if (this.toolkit && this.xml) this._layout(); };
    window.addEventListener("resize", () => { window.clearTimeout(this._resizeTimer); this._resizeTimer = window.setTimeout(this._resize, 250); });
  }

  async load(musicxml, findings = []) {
    this.xml = musicxml;
    this.findings = findings;
    this.status.textContent = "Drawing the score…";
    try {
      this.toolkit = await loadEngine();
      this._layout();
      this.status.textContent = "";
    } catch (error) {
      this.status.textContent = error.message;
      replace(this.canvas);
    }
  }

  _layout() {
    const width = Math.max(320, this.canvas.clientWidth || 800);
    this.toolkit.setOptions({
      pageWidth: Math.round((width * 100) / this.zoom), pageHeight: Math.round((1100 * 100) / this.zoom),
      scale: this.zoom, adjustPageHeight: true, breaks: "auto", footer: "none", header: "none",
      pageMarginLeft: 40, pageMarginRight: 40, pageMarginTop: 40, pageMarginBottom: 40,
    });
    if (!this.toolkit.loadData(this.xml)) throw new Error("The score could not be drawn.");
    this.pages = this.toolkit.getPageCount();
    this.show(Math.min(this.page, this.pages));
  }

  setZoom(value) {
    this.zoom = Math.max(24, Math.min(72, value));
    if (this.toolkit && this.xml) this._layout();
  }

  show(page) {
    if (!this.toolkit) return;
    this.page = Math.max(1, Math.min(this.pages, page));
    replace(this.canvas, sanitise(this.toolkit.renderToSVG(this.page)));
    this.pageLabel.textContent = `Page ${this.page} of ${this.pages}`;
    this.prev.disabled = this.page <= 1;
    this.next.disabled = this.page >= this.pages;
    this._mark();
  }

  // Findings are marked with a shape as well as a colour: HARD notes get a
  // solid ring, STRAIN notes a dashed one. The list beside the score carries
  // the same information as text.
  _mark() {
    for (const finding of this.findings) {
      for (const id of finding.note_ids || []) {
        const el = this.canvas.querySelector(`[id="${CSS.escape(id)}"]`);
        if (!el || el.querySelector(".finding-ring")) continue;
        const box = el.getBBox();
        const ring = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        const pad = Math.max(box.width, box.height) * 0.35;
        ring.setAttribute("x", box.x - pad); ring.setAttribute("y", box.y - pad);
        ring.setAttribute("width", box.width + 2 * pad); ring.setAttribute("height", box.height + 2 * pad);
        ring.setAttribute("rx", pad);
        ring.setAttribute("class", `finding-ring finding-${finding.severity}`);
        const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
        title.textContent = finding.message;
        ring.append(title);
        el.append(ring);
      }
    }
  }

  reveal(finding) {
    if (!this.toolkit) return;
    const id = (finding.note_ids || []).find((noteId) => this.toolkit.getPageWithElement(noteId) > 0);
    if (!id) { announce(`Bar ${finding.bar} is not marked on the page; see the list.`); return; }
    this.show(this.toolkit.getPageWithElement(id));
    const el = this.canvas.querySelector(`[id="${CSS.escape(id)}"]`);
    if (el) {
      el.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      el.classList.add("finding-focus");
      window.setTimeout(() => el.classList.remove("finding-focus"), 2500);
    }
    announce(`Showing bar ${finding.bar} on page ${this.page}.`);
  }
}
