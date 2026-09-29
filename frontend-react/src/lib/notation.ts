// Notation engine: Verovio renders the same MusicXML the user downloads.
//
// The engine is 7 MB of WebAssembly, so it is loaded the first time a score is
// shown, not at page load. Its SVG is parsed as a document and adopted node by
// node after scripts, foreignObject and event-handler attributes are removed;
// innerHTML is never used. Note ids in the MusicXML survive into the SVG, which
// is what lets a finding point at its exact notes.

export const VEROVIO_VERSION = "6.3.0";   // keep in step with fetch_vendor.py

export interface Toolkit {
  setOptions(options: Record<string, unknown>): void;
  loadData(data: string): boolean;
  getPageCount(): number;
  renderToSVG(page: number): string;
  getPageWithElement(id: string): number;
}

interface VerovioGlobal {
  verovio?: { toolkit: new () => Toolkit; module?: { calledRun?: boolean; onRuntimeInitialized?: () => void } };
}

let enginePromise: Promise<Toolkit> | null = null;

export function loadEngine(): Promise<Toolkit> {
  if (enginePromise) return enginePromise;
  enginePromise = new Promise<Toolkit>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `vendor/verovio-toolkit-wasm.js?v=${VEROVIO_VERSION}`;   // versioned: the server lets it be cached for a week
    script.onerror = () => { enginePromise = null; reject(new Error("The notation engine could not be loaded. Run fetch_vendor.py on the server.")); };
    script.onload = () => {
      const g = window as unknown as VerovioGlobal;
      const ready = () => resolve(new g.verovio!.toolkit());
      if (g.verovio && g.verovio.module && g.verovio.module.calledRun) ready();
      else if (g.verovio && g.verovio.module) g.verovio.module.onRuntimeInitialized = ready;
      else reject(new Error("The notation engine did not start."));
    };
    document.head.append(script);
  });
  return enginePromise;
}

// Verovio puts its own CSS inside the SVG: element rules scoped to the SVG's
// id, and its music font as an embedded @font-face. The content security
// policy allows no inline styles, and rightly so, but it does not govern
// stylesheets built through the CSSOM. So the <style> elements are lifted out
// of the SVG and their text goes into one constructed stylesheet, replaced on
// every render. The rules stay scoped to each SVG's id, so several scores on
// one page do not interfere.
const svgStyles = new Map<Element, string>();   // canvas element -> css text of the score it shows
let sheet: CSSStyleSheet | null = null;

function applySvgStyles(canvas: Element, text: string): void {
  svgStyles.set(canvas, text);
  for (const known of svgStyles.keys()) if (!known.isConnected && known !== canvas) svgStyles.delete(known);
  if (typeof CSSStyleSheet === "undefined" || !("replaceSync" in CSSStyleSheet.prototype)) return;
  if (!sheet) {
    sheet = new CSSStyleSheet();
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  }
  sheet.replaceSync(Array.from(svgStyles.values()).join("\n"));
}

const STYLE_ELEMENT = /<style\b[^>]*>([\s\S]*?)<\/style>/gi;

export function cssText(raw: string): string {
  return raw.replace(/^\s*<!\[CDATA\[/, "").replace(/\]\]>\s*$/, "")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
}

export function sanitise(svgText: string, canvas: Element): Element {
  // The <style> elements come out of the text before it is parsed: a parsed
  // document inherits the page's policy, and the browser reports the inline
  // styles as blocked at parse time even though nothing in it is ever live.
  const css: string[] = [];
  const stripped = svgText.replace(STYLE_ELEMENT, (_, text: string) => { css.push(cssText(text)); return ""; });
  const parsed = new DOMParser().parseFromString(stripped, "image/svg+xml");
  if (parsed.querySelector("parsererror")) throw new Error("The score could not be drawn.");
  for (const el of parsed.querySelectorAll("script, foreignObject, iframe, object, embed, style")) el.remove();
  for (const el of parsed.querySelectorAll("*")) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on") || name === "style" || ((name === "href" || name === "xlink:href") && !attr.value.startsWith("#"))) el.removeAttribute(attr.name);
    }
  }
  applySvgStyles(canvas, css.join("\n"));
  return document.importNode(parsed.documentElement, true);
}

export interface Marked { severity: string; message: string; note_ids?: string[]; bar?: number | null }

// Findings are marked with a shape as well as a colour: HARD notes get a
// solid ring, STRAIN notes a dashed one. The list beside the score carries
// the same information as text.
export function markFindings(canvas: Element, findings: Marked[]): void {
  for (const finding of findings) {
    for (const id of finding.note_ids || []) {
      const el = canvas.querySelector(`[id="${CSS.escape(id)}"]`) as SVGGraphicsElement | null;
      if (!el || el.querySelector(".finding-ring") || typeof el.getBBox !== "function") continue;
      const box = el.getBBox();
      const ring = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const pad = Math.max(box.width, box.height) * 0.35;
      ring.setAttribute("x", String(box.x - pad)); ring.setAttribute("y", String(box.y - pad));
      ring.setAttribute("width", String(box.width + 2 * pad)); ring.setAttribute("height", String(box.height + 2 * pad));
      ring.setAttribute("rx", String(pad));
      ring.setAttribute("class", `finding-ring finding-${finding.severity}`);
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = finding.message;
      ring.append(title);
      el.append(ring);
    }
  }
}

export function layoutOptions(width: number, zoom: number): Record<string, unknown> {
  return {
    pageWidth: Math.round((width * 100) / zoom), pageHeight: Math.round((1100 * 100) / zoom),
    scale: zoom, adjustPageHeight: true, breaks: "auto", footer: "none", header: "none",
    pageMarginLeft: 40, pageMarginRight: 40, pageMarginTop: 40, pageMarginBottom: 40,
  };
}

export const MIN_ZOOM = 24;
export const MAX_ZOOM = 72;
export const DEFAULT_ZOOM = 40;

export function clampZoom(value: number): number {
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
}
