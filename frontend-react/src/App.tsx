// Application shell: session, routing, navigation, and the signed-out views.
import { useEffect } from "react";
import { client } from "./api/client";
import { go, parseRoute, useHashRoute } from "./lib/route";
import { useSession } from "./lib/session";
import { useToast } from "./lib/toast";
import { Account } from "./views/Account";
import { Auth } from "./views/Auth";
import { Home } from "./views/Home";
import { Library } from "./views/Library";
import { Forgot, Reset, VerifyEmail } from "./views/Password";
import { ProjectView } from "./views/project/ProjectView";

const NEEDS_USER = ["library", "project", "account"];

export function Nav({ current }: { current: string }) {
  const { user, setUser } = useSession();
  const { toast } = useToast();

  async function signOut() {
    try { await client.POST("/auth/logout"); } catch { /* the session may already be gone */ }
    setUser(null);
    toast("Signed out.", "success");
    go("/");
  }

  const link = (href: string, text: string, key: string) => (
    <a href={href} aria-current={current === key ? "page" : undefined} data-testid={`nav-${key || "home"}`}>{text}</a>
  );
  return user ? (
    <>
      {link("#/library", "Your pieces", "library")}
      {link("#/account", "Account", "account")}
      <button type="button" className="link-button" data-testid="nav-sign-out" onClick={() => void signOut()}>Sign out</button>
    </>
  ) : (
    <>
      {link("#/", "About", "")}
      {link("#/login", "Sign in", "login")}
      {link("#/register", "Create account", "register")}
    </>
  );
}

export function View({ parts, query }: { parts: string[]; query: URLSearchParams }) {
  const [section, id] = parts;
  const { user } = useSession();
  if (NEEDS_USER.includes(section) && !user) return null;
  if (section === "library") return <Library />;
  if (section === "project" && id) return <ProjectView key={id} projectId={id} />;
  if (section === "account") return <Account />;
  if (section === "login") return <Auth key="login" mode="login" />;
  if (section === "register") return <Auth key="register" mode="register" />;
  if (section === "forgot") return <Forgot />;
  if (section === "reset-password") return <Reset token={query.get("token")} />;
  if (section === "verify-email") return <VerifyEmail token={query.get("token")} />;
  return <Home />;
}

export function App() {
  const { ready, user } = useSession();
  const route = useHashRoute();
  const section = route.parts[0] || "";

  // The address fragment is the route here, so the skip link cannot be a plain
  // "#main" jump: it would navigate. Move focus instead, which is what it is for.
  function skip(event: React.MouseEvent) {
    event.preventDefault();
    const main = document.getElementById("main");
    if (main) { main.focus(); if (typeof main.scrollIntoView === "function") main.scrollIntoView(); }
  }

  useEffect(() => {
    if (!ready) return;
    if (!window.location.hash) { window.location.hash = user ? "/library" : "/"; return; }
    // The live address, not the route state: signing out navigates home in the
    // same tick, and the state only catches up on the hashchange event.
    const live = parseRoute().parts[0] || "";
    if (NEEDS_USER.includes(live) && !user) go("/login");
  }, [ready, user, section]);

  return (
    <>
      <a className="skip-link" href="#main" onClick={skip}>Skip to content</a>
      <header className="site-header">
        <a className="brand" href="#/">Arranger</a>
        <nav id="nav" aria-label="Main">{ready ? <Nav current={section} /> : null}</nav>
      </header>
      <main id="main" tabIndex={-1}>
        {ready ? <View parts={route.parts} query={route.query} /> : <><h1>Arranger</h1><p>Loading…</p></>}
      </main>
      <footer className="site-footer">
        <nav aria-label="Legal and help">
          <a href="privacy.html">Privacy</a>
          <a href="terms.html">Terms</a>
          <a href="cookies.html">Cookies</a>
          <a href="copyright.html">Upload rights</a>
          <a href="support.html">Support</a>
        </nav>
        <p className="muted">Notation is drawn by Verovio (LGPL). PDFs are engraved by LilyPond (GPL).</p>
      </footer>
    </>
  );
}
