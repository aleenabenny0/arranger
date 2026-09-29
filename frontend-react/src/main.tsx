import { createRoot } from "react-dom/client";
import { App } from "./App";
import { SessionProvider } from "./lib/session";
import { ToastProvider } from "./lib/toast";

createRoot(document.getElementById("root")!).render(
  <SessionProvider>
    <ToastProvider>
      <App />
    </ToastProvider>
  </SessionProvider>,
);
