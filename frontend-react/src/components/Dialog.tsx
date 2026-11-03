// A modal built on <dialog>: the browser traps focus and handles Escape, and
// focus goes back to whatever opened it when it closes.
import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface DialogProps {
  title: string;
  children: ReactNode;
  actions: ReactNode;
  onClose: () => void;
}

export function Dialog({ title, children, actions, onClose }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const closed = useRef(onClose);
  closed.current = onClose;

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const opener = document.activeElement as HTMLElement | null;
    if (typeof el.showModal === "function" && !el.open) el.showModal();
    else el.setAttribute("open", "");
    const onCloseEvent = () => closed.current();
    el.addEventListener("close", onCloseEvent);
    return () => {
      el.removeEventListener("close", onCloseEvent);
      if (el.open && typeof el.close === "function") el.close();
      if (opener && opener.isConnected) opener.focus();
    };
  }, []);

  return createPortal(
    <dialog ref={ref} aria-labelledby={titleId} className="dialog" data-testid="dialog">
      <h2 id={titleId}>{title}</h2>
      {children}
      <div className="dialog-actions">{actions}</div>
    </dialog>,
    document.body,
  );
}

interface ConfirmProps {
  title: string;
  message: string;
  confirmLabel: string;
  danger?: boolean;
  onResult: (ok: boolean) => void;
}

export function ConfirmDialog({ title, message, confirmLabel, danger = false, onResult }: ConfirmProps) {
  const decided = useRef(false);
  const decide = (ok: boolean) => { if (decided.current) return; decided.current = true; onResult(ok); };
  return (
    <Dialog title={title} onClose={() => decide(false)} actions={
      <>
        <button type="button" className="button" onClick={() => decide(false)}>Cancel</button>
        <button type="button" className={danger ? "button button-danger" : "button button-primary"} onClick={() => decide(true)}>{confirmLabel}</button>
      </>
    }>
      <p>{message}</p>
    </Dialog>
  );
}
