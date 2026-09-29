// Status messages: a visible toast and a live-region announcement, kept
// separate so re-rendering one never re-announces the other. Polite for
// progress, assertive for errors.
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

export type ToastKind = "info" | "success" | "error";

export interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

interface ToastApi {
  toast: (message: string, kind?: ToastKind) => void;
  announce: (message: string, options?: { urgent?: boolean }) => void;
}

const ToastContext = createContext<ToastApi>({ toast: () => undefined, announce: () => undefined });

export function useToast(): ToastApi {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [polite, setPolite] = useState("");
  const [assertive, setAssertive] = useState("");
  const counter = useRef(0);

  const announce = useCallback((message: string, { urgent = false }: { urgent?: boolean } = {}) => {
    const set = urgent ? setAssertive : setPolite;
    set("");
    window.setTimeout(() => set(message), 30);
  }, []);

  const dismiss = useCallback((id: number) => setToasts((current) => current.filter((t) => t.id !== id)), []);

  const toast = useCallback((message: string, kind: ToastKind = "info") => {
    const label = kind === "error" ? "Error: " : kind === "success" ? "Done: " : "";
    counter.current += 1;
    const item = { id: counter.current, kind, text: label + message };
    // Never bury the page under messages.
    setToasts((current) => [...current.slice(-2), item]);
    announce(label + message, { urgent: kind === "error" });
    window.setTimeout(() => dismiss(item.id), kind === "error" ? 12000 : 6000);
  }, [announce, dismiss]);

  const api = useMemo(() => ({ toast, announce }), [toast, announce]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div id="toasts" className="toasts" role="region" aria-label="Notifications">
        {toasts.map((item) => (
          <div key={item.id} className={`toast toast-${item.kind}`} data-testid={`toast-${item.kind}`}>
            <span className="toast-icon" aria-hidden="true">{item.kind === "error" ? "!" : item.kind === "success" ? "✓" : "i"}</span>
            <span>{item.text}</span>
            <button type="button" className="icon-button" aria-label="Dismiss message" onClick={() => dismiss(item.id)}>×</button>
          </div>
        ))}
      </div>
      <div id="live-polite" className="visually-hidden" role="status" aria-live="polite">{polite}</div>
      <div id="live-assertive" className="visually-hidden" role="alert" aria-live="assertive">{assertive}</div>
    </ToastContext.Provider>
  );
}
