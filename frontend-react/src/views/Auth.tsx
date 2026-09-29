// Sign in and create account: one form, two modes. Errors are shown in words
// next to the form and announced; the page never navigates on failure.
import { useRef, useState, type FormEvent } from "react";
import { client } from "../api/client";
import type { User } from "../api/types";
import { Field } from "../components/Field";
import { setTitle, useFocusHeading } from "../lib/focus";
import { go } from "../lib/route";
import { useSession } from "../lib/session";
import { useToast } from "../lib/toast";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validEmail(value: string): boolean {
  return EMAIL_PATTERN.test(value.trim());
}

export function Auth({ mode }: { mode: "login" | "register" }) {
  const isRegister = mode === "register";
  const { setUser } = useSession();
  const { toast, announce } = useToast();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [emailError, setEmailError] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  setTitle(isRegister ? "Create account" : "Sign in");
  useFocusHeading(true, mode);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setEmailError(""); setPasswordError(""); setFormError("");
    if (!validEmail(email)) { setEmailError("Enter a valid email address."); emailRef.current?.focus(); return; }
    if (!password || (isRegister && password.length < 10)) {
      setPasswordError(isRegister ? "Use at least 10 characters." : "Enter your password.");
      passwordRef.current?.focus();
      return;
    }
    setBusy(true);
    try {
      const body = { email: email.trim(), password };
      const { data } = isRegister
        ? await client.POST("/auth/register", { body: { ...body, display_name: name.trim() } })
        : await client.POST("/auth/login", { body });
      setUser(data ? (data.user as unknown as User) : null);
      setPassword("");
      toast(isRegister ? "Account created. Check your email to verify your address." : "Signed in.", "success");
      go("/library");
    } catch (error) {
      const message = (error as Error).message;
      setFormError(message);
      announce(message, { urgent: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="narrow">
      <h1>{isRegister ? "Create your account" : "Sign in"}</h1>
      <form className="form" noValidate onSubmit={submit}>
        <Field label="Email address" error={emailError}>
          <input ref={emailRef} type="email" name="email" autoComplete="email" required inputMode="email" data-testid="auth-email"
            value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        {isRegister ? (
          <Field label="Name (optional)">
            <input type="text" name="display_name" autoComplete="name" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        ) : null}
        <Field label="Password" error={passwordError} hint={isRegister ? "At least 10 characters. A few unrelated words works well." : undefined}>
          <input ref={passwordRef} type="password" name="password" required minLength={isRegister ? 10 : 1}
            autoComplete={isRegister ? "new-password" : "current-password"} data-testid="auth-password"
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <p className="form-error" role="alert" data-testid="auth-error" hidden={!formError}>{formError}</p>
        <button type="submit" className="button button-primary" disabled={busy} data-testid="auth-submit">{isRegister ? "Create account" : "Sign in"}</button>
      </form>
      <p>
        {isRegister
          ? <>Already have an account? <a href="#/login">Sign in</a></>
          : <><a href="#/forgot">Forgot your password?</a> · <a href="#/register">Create an account</a></>}
      </p>
      {isRegister ? (
        <p className="muted">By creating an account you agree to the <a href="terms.html">terms</a> and have read the <a href="privacy.html">privacy notice</a>.</p>
      ) : null}
    </div>
  );
}
