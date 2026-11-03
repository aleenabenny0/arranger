// Forgotten password: request a link, then choose a new password from it.
// The token lives in the address fragment and is removed as soon as it is read.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { client } from "../api/client";
import type { User } from "../api/types";
import { Field } from "../components/Field";
import { setTitle, useFocusHeading } from "../lib/focus";
import { go, scrubHash } from "../lib/route";
import { useSession } from "../lib/session";
import { useToast } from "../lib/toast";
import { validEmail } from "./Auth";

export function Forgot() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [busy, setBusy] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  setTitle("Reset password");
  useFocusHeading(true, "forgot");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!validEmail(email)) { setError("Enter a valid email address."); emailRef.current?.focus(); return; }
    setError(""); setBusy(true);
    try {
      await client.POST("/auth/password-reset/request", { body: { email: email.trim() } });
      setDone("If that address has an account, a reset link is on its way. It expires soon.");
    } catch (failure) {
      setDone((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="narrow">
      <h1>Reset your password</h1>
      <form className="form" noValidate onSubmit={submit}>
        <Field label="Email address" error={error}>
          <input ref={emailRef} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <button type="submit" className="button button-primary" disabled={busy}>Send reset link</button>
      </form>
      <p role="status">{done}</p>
      <p><a href="#/login">Back to sign in</a></p>
    </div>
  );
}

export function Reset({ token }: { token: string | null }) {
  const { setUser } = useSession();
  const { toast } = useToast();
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [againError, setAgainError] = useState("");
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);
  const againRef = useRef<HTMLInputElement>(null);
  setTitle("Choose a new password");
  useFocusHeading(true, "reset");
  useEffect(() => { scrubHash("/reset-password"); }, []);

  if (!token) {
    return (
      <div className="narrow">
        <h1>This link is incomplete</h1>
        <p>Open the link from your email again, or request a new one.</p>
        <a href="#/forgot">Request a new link</a>
      </div>
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setPasswordError(""); setAgainError(""); setFormError("");
    if (password.length < 10) { setPasswordError("Use at least 10 characters."); passwordRef.current?.focus(); return; }
    if (password !== again) { setAgainError("The two passwords do not match."); againRef.current?.focus(); return; }
    setBusy(true);
    try {
      const { data } = await client.POST("/auth/password-reset/confirm", { body: { token: token as string, password } });
      setUser(data ? (data.user as unknown as User) : null);
      setPassword(""); setAgain("");
      toast("Password changed. Other devices were signed out.", "success");
      go("/library");
    } catch (failure) {
      setFormError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="narrow">
      <h1>Choose a new password</h1>
      <form className="form" noValidate onSubmit={submit}>
        <Field label="New password" hint="At least 10 characters." error={passwordError}>
          <input ref={passwordRef} type="password" autoComplete="new-password" required minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="New password again" error={againError}>
          <input ref={againRef} type="password" autoComplete="new-password" required value={again} onChange={(e) => setAgain(e.target.value)} />
        </Field>
        <p className="form-error" role="alert" hidden={!formError}>{formError}</p>
        <button type="submit" className="button button-primary" disabled={busy}>Set new password</button>
      </form>
    </div>
  );
}

export function VerifyEmail({ token }: { token: string | null }) {
  const { setUser } = useSession();
  const [state, setState] = useState<{ status: "working" | "done" | "failed" | "missing"; message?: string }>(() => ({ status: token ? "working" : "missing" }));
  setTitle("Verify email");
  useFocusHeading(state.status !== "working", `verify-${state.status}`);

  useEffect(() => {
    scrubHash("/verify-email");
    if (!token) return undefined;
    let cancelled = false;
    client.POST("/auth/email/verify", { body: { token } })
      .then(({ data }) => { if (!cancelled) { setUser(data ? (data.user as unknown as User) : null); setState({ status: "done" }); } })
      .catch((error: Error) => { if (!cancelled) setState({ status: "failed", message: error.message }); });
    return () => { cancelled = true; };
  }, [token, setUser]);

  if (state.status === "missing") {
    return <div className="narrow"><h1>This link is incomplete</h1><p>Open the link from your email again.</p></div>;
  }
  if (state.status === "working") {
    return <div className="narrow"><h1>Verifying your email</h1><p role="status">One moment…</p></div>;
  }
  if (state.status === "done") {
    return (
      <div className="narrow">
        <h1>Email verified</h1>
        <p>Thank you. You can use every feature now.</p>
        <a className="button button-primary" href="#/library">Go to your pieces</a>
      </div>
    );
  }
  return (
    <div className="narrow">
      <h1>That link did not work</h1>
      <p role="alert">{state.message}</p>
      <p>Links expire and work once. Sign in and ask for a new one from your account page.</p>
    </div>
  );
}
