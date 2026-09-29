// Notation preview. The Verovio SVG is adopted into a plain element the React
// tree never re-renders, so the marks a finding adds survive.
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { clampZoom, DEFAULT_ZOOM, layoutOptions, loadEngine, markFindings, sanitise, type Marked, type Toolkit } from "../lib/notation";
import { useToast } from "../lib/toast";

export interface NotationHandle { reveal: (finding: Marked) => void }

interface NotationProps {
  label: string;
  musicxml: string | null;
  findings?: Marked[];
}

export const Notation = forwardRef<NotationHandle, NotationProps>(function Notation({ label, musicxml, findings = [] }, handle) {
  const canvas = useRef<HTMLDivElement>(null);
  const toolkit = useRef<Toolkit | null>(null);
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [zoom, setZoom] = useState(DEFAULT_ZOOM);
  const findingsRef = useRef(findings);
  findingsRef.current = findings;
  const { announce } = useToast();

  const show = useCallback((wanted: number, total = pages) => {
    const tk = toolkit.current;
    const host = canvas.current;
    if (!tk || !host) return;
    const target = Math.max(1, Math.min(total, wanted));
    while (host.firstChild) host.removeChild(host.firstChild);
    host.append(sanitise(tk.renderToSVG(target), host));
    markFindings(host, findingsRef.current);
    setPage(target);
  }, [pages]);

  const layout = useCallback((xml: string, scale: number, keepPage: number) => {
    const tk = toolkit.current;
    const host = canvas.current;
    if (!tk || !host) return;
    const width = Math.max(320, host.clientWidth || 800);
    tk.setOptions(layoutOptions(width, scale));
    if (!tk.loadData(xml)) throw new Error("The score could not be drawn.");
    const total = tk.getPageCount();
    setPages(total);
    show(Math.min(keepPage, total), total);
  }, [show]);

  useEffect(() => {
    let cancelled = false;
    if (!musicxml) return undefined;
    setStatus("Drawing the score…");
    loadEngine().then((tk) => {
      if (cancelled) return;
      toolkit.current = tk;
      layout(musicxml, zoom, 1);
      setStatus("");
    }).catch((error: Error) => {
      if (cancelled) return;
      setStatus(error.message);
      const host = canvas.current;
      if (host) while (host.firstChild) host.removeChild(host.firstChild);
    });
    return () => { cancelled = true; };
    // Zoom changes re-layout through their own handler; only a new score reloads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [musicxml, layout]);

  useEffect(() => {
    let timer = 0;
    const onResize = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => { if (toolkit.current && musicxml) layout(musicxml, zoom, page); }, 250);
    };
    window.addEventListener("resize", onResize);
    return () => { window.removeEventListener("resize", onResize); window.clearTimeout(timer); };
  }, [musicxml, zoom, page, layout]);

  function changeZoom(next: number) {
    const clamped = clampZoom(next);
    setZoom(clamped);
    if (toolkit.current && musicxml) layout(musicxml, clamped, page);
  }

  useImperativeHandle(handle, () => ({
    reveal(finding: Marked) {
      const tk = toolkit.current;
      const host = canvas.current;
      if (!tk || !host) return;
      const id = (finding.note_ids || []).find((noteId) => tk.getPageWithElement(noteId) > 0);
      if (!id) { announce(`Bar ${finding.bar} is not marked on the page; see the list.`); return; }
      const target = tk.getPageWithElement(id);
      show(target);
      const el = host.querySelector(`[id="${CSS.escape(id)}"]`);
      if (el) {
        const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
        el.classList.add("finding-focus");
        window.setTimeout(() => el.classList.remove("finding-focus"), 2500);
      }
      announce(`Showing bar ${finding.bar} on page ${target}.`);
    },
  }), [announce, show]);

  return (
    <section className="notation">
      <div className="toolbar" role="group" aria-label="Score pages">
        <button type="button" className="button" disabled={page <= 1} onClick={() => show(page - 1)}>Previous page</button>
        <span className="page-label" aria-live="polite">{`Page ${page} of ${pages}`}</span>
        <button type="button" className="button" disabled={page >= pages} onClick={() => show(page + 1)}>Next page</button>
        <button type="button" className="button" aria-label="Smaller notes" onClick={() => changeZoom(zoom - 8)}>A−</button>
        <button type="button" className="button" aria-label="Larger notes" onClick={() => changeZoom(zoom + 8)}>A+</button>
      </div>
      <p className="muted" role="status">{status}</p>
      <div ref={canvas} className="notation-canvas" role="img" aria-label={label} tabIndex={0} />
    </section>
  );
});
