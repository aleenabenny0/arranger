// Account: what is stored, who is signed in, password, export, deletion.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, BASE, client } from "../api/client";
import type { SessionRow, Usage } from "../api/types";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Field } from "../components/Field";
import { setTitle, useFocusHeading } from "../lib/focus";
import { formatBytes, formatDate } from "../lib/format";
import { go } from "../lib/route";
import { useSession } from "../lib/session";
import { useToast } from "../lib/toast";

function MeterRow({ label, used, limit, format = String }: { label: string; used: number; limit: number; format?: (n: number) => string }) {
  return (
    <li>
      <span>{label}</span>
      <meter min={0} max={limit} high={limit * 0.8} value={Math.min(used, limit)} aria-label={label} />
      <span>{`${format(used)} of ${format(limit)}`}</span>
    </li>
  );
}

export function retentionText(retention: Usage["retention"]): string {
  const uploads = retention.upload_days ? `${retention.upload_days} days` : "until you delete the piece";
  const exports = retention.export_days ? `${retention.export_days} days and can be made again` : "until you delete the piece";
  return `Uploaded originals are kept ${uploads}. Generated files are kept ${exports}.`;
}

function DeleteAccountDialog({ onClose, onDeleted }: { onClose: () => void; onDeleted: () => void }) {
  const [password, setPassword] = useState("");
  const [word, setWord] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [wordError, setWordError] = useState("");
  const [busy, setBusy] = useState(false);
  const wordRef = useRef<HTMLInputElement>(null);

  async function confirm() {
    setPasswordError(""); setWordError("");
    if (word !== "DELETE") { setWordError("Type DELETE in capitals."); wordRef.current?.focus(); return; }
    setBusy(true);
    try {
      await client.DELETE("/account", { body: { password, confirm: "DELETE" } });
      onDeleted();
    } catch (error) {
      setPasswordError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title="Delete your account?" onClose={onClose} actions={
      <>
        <button type="button" className="button" onClick={onClose}>Keep my account</button>
        <button type="button" className="button button-danger" disabled={busy} onClick={() => void confirm()}>Delete everything</button>
      </>
    }>
      <div className="form">
        <p>Your pieces, arrangements, files and account are removed from this service. This cannot be undone. Download a copy first if you want one.</p>
        <Field label="Your password" error={passwordError}>
          <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Type DELETE to confirm" error={wordError}>
          <input ref={wordRef} type="text" autoComplete="off" required value={word} onChange={(e) => setWord(e.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}

export function Account() {
  const { user, setUser } = useSession();
  const { toast } = useToast();
  const [usage, setUsage] = useState<Usage | null>(null);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [currentError, setCurrentError] = useState("");
  const [nextError, setNextError] = useState("");
  const [busy, setBusy] = useState(false);
  const [resending, setResending] = useState(false);
  const [confirmingSignOut, setConfirmingSignOut] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const currentRef = useRef<HTMLInputElement>(null);
  const nextRef = useRef<HTMLInputElement>(null);
  setTitle("Account");
  useFocusHeading(usage !== null || error !== null, "account");

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      client.GET("/account/usage", { signal: controller.signal }),
      client.GET("/auth/sessions", { signal: controller.signal }),
    ]).then(([usageResponse, sessionsResponse]) => {
      setUsage(usageResponse.data as unknown as Usage);
      setSessions(sessionsResponse.data ? (sessionsResponse.data.sessions as unknown as SessionRow[]) : []);
    }).catch((failure: ApiError) => {
      if (failure.aborted) return;
      if (failure.status === 401) { setUser(null); go("/login"); return; }
      setError(failure);
    });
    return () => controller.abort();
  }, [setUser]);

  async function resend() {
    setResending(true);
    try { await client.POST("/auth/email/resend"); toast("Sent. Check your inbox.", "success"); }
    catch (failure) { toast((failure as Error).message, "error"); }
    finally { setResending(false); }
  }

  async function changePassword(event: FormEvent) {
    event.preventDefault();
    setCurrentError(""); setNextError("");
    if (!current) { setCurrentError("Enter your current password."); currentRef.current?.focus(); return; }
    if (next.length < 10) { setNextError("Use at least 10 characters."); nextRef.current?.focus(); return; }
    setBusy(true);
    try {
      await client.POST("/auth/password/change", { body: { current_password: current, new_password: next } });
      setCurrent(""); setNext("");
      toast("Password changed. Other devices were signed out.", "success");
    } catch (failure) {
      const apiError = failure as ApiError;
      if (apiError.status === 403 || apiError.status === 401) setCurrentError(apiError.message); else setNextError(apiError.message);
    } finally {
      setBusy(false);
    }
  }

  async function signOutDevice(session: SessionRow) {
    try {
      await client.DELETE("/auth/sessions/{session_id}", { params: { path: { session_id: session.id } } });
      setSessions((rows) => rows.filter((row) => row.id !== session.id));
      toast("That device was signed out.", "success");
    } catch (failure) {
      toast((failure as Error).message, "error");
    }
  }

  async function signOutEverywhere() {
    try { await client.POST("/auth/logout-all"); setUser(null); go("/login"); }
    catch (failure) { toast((failure as Error).message, "error"); }
  }

  if (error) {
    return (
      <>
        <h1>Something went wrong</h1>
        <p role="alert">{error.message}</p>
        <p><a href="#/library">Back to your pieces</a></p>
      </>
    );
  }
  if (!usage || !user) return <><h1>Account</h1><p role="status">Loading…</p></>;

  return (
    <>
      <h1>Account</h1>
      <section className="panel" aria-labelledby="verify-h">
        <h2 id="verify-h">Email address</h2>
        <p>{`${user.email} · ${user.email_verified ? "verified" : "not verified yet"}`}</p>
        {!user.email_verified ? <button type="button" className="button" disabled={resending} onClick={() => void resend()}>Send the verification link again</button> : null}
      </section>
      <section className="panel" aria-labelledby="usage-h">
        <h2 id="usage-h">What you are storing</h2>
        <ul className="meters">
          <MeterRow label="Pieces" used={usage.projects.used} limit={usage.projects.limit} />
          <MeterRow label="Files" used={usage.storage_bytes.used} limit={usage.storage_bytes.limit} format={formatBytes} />
          <MeterRow label="Jobs in the last day" used={usage.jobs_today.used} limit={usage.jobs_today.limit} />
        </ul>
        <p className="muted">{retentionText(usage.retention)}</p>
      </section>
      <section className="panel" aria-labelledby="pw-h">
        <h2 id="pw-h">Password</h2>
        <form className="form" noValidate onSubmit={changePassword}>
          <Field label="Current password" error={currentError}>
            <input ref={currentRef} type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
          </Field>
          <Field label="New password" hint="At least 10 characters." error={nextError}>
            <input ref={nextRef} type="password" autoComplete="new-password" required minLength={10} value={next} onChange={(e) => setNext(e.target.value)} />
          </Field>
          <button type="submit" className="button" disabled={busy}>Change password</button>
        </form>
      </section>
      <section className="panel" aria-labelledby="sess-h">
        <h2 id="sess-h">Where you are signed in</h2>
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Table, scroll sideways if it is cut off">
          <table>
            <thead><tr>{["Device", "Network", "Last used", "Sign out"].map((t) => <th key={t} scope="col">{t}</th>)}</tr></thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id}>
                  <th scope="row">{s.current ? "This device" : (s.user_agent || "Unknown device").slice(0, 60)}</th>
                  <td>{s.ip || s.ip_address || ""}</td>
                  <td>{formatDate(s.last_seen_at || s.created_at)}</td>
                  <td>{s.current ? null : <button type="button" className="button button-small" onClick={() => void signOutDevice(s)}>Sign out</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <button type="button" className="button" onClick={() => setConfirmingSignOut(true)}>Sign out everywhere</button>
      </section>
      <section className="panel" aria-labelledby="data-h">
        <h2 id="data-h">Your data</h2>
        <p>You can take a copy of everything stored about you, or delete it all.</p>
        <div className="toolbar">
          <button type="button" className="button" onClick={() => window.location.assign(`${BASE}/account/export`)}>Download a copy of my data</button>
          <button type="button" className="button button-danger" onClick={() => setDeleting(true)}>Delete my account</button>
        </div>
        <p className="muted"><a href="privacy.html">Privacy notice</a></p>
      </section>
      {confirmingSignOut ? (
        <ConfirmDialog title="Sign out everywhere?" message="Every device, including this one, will be signed out." confirmLabel="Sign out everywhere"
          onResult={(ok) => { setConfirmingSignOut(false); if (ok) void signOutEverywhere(); }} />
      ) : null}
      {deleting ? (
        <DeleteAccountDialog onClose={() => setDeleting(false)} onDeleted={() => {
          setDeleting(false); setUser(null); toast("Your account and data were deleted.", "success"); go("/");
        }} />
      ) : null}
    </>
  );
}
