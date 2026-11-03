// The plan editor and the raw JSON views. The plan is a list of decisions,
// not notes: editing it and evaluating makes a new revision on the server.
// Two checks run in the browser before anything is sent: the plan's shape
// (validatePlan) and, on request, the current notes against the current hand
// profile (verifyScore), which says whether a changed profile would still
// accept this arrangement without arranging again.
import { useMemo, useState } from "react";
import type { Arrangement, Playback } from "../../api/types";
import { Field } from "../../components/Field";
import { validatePlan, verifyScore, type HandProfile, type Plan, type Score, type Verdict } from "../../lib/verify";
import type { Profile } from "./constants";

interface AdvancedPanelProps {
  arrangement: Arrangement | null;
  playback: Playback | null;
  profile: Profile;
  onEvaluate: (plan: Record<string, unknown>) => void;
}

export function scoreFromPlayback(playback: Playback, title: string): Score {
  return {
    title,
    notes: playback.notes.map((n) => ({ pitch: n[0], onset: n[1], duration: n[2], velocity: n[3], staff: n[4], id: n[5], bar: n[6] })),
  };
}

export function handProfile(profile: Profile): HandProfile {
  const number = (key: string, fallback: number) => (typeof profile[key] === "number" ? (profile[key] as number) : fallback);
  return {
    max_span: number("max_span", 12), comfortable_span: number("comfortable_span", 10), max_notes_per_hand: number("max_notes_per_hand", 4),
    max_leap_rate: number("max_leap_rate", 70), leap_slack: number("leap_slack", 5),
    lowest_pitch: number("lowest_pitch", 21), highest_pitch: number("highest_pitch", 108),
  };
}

export function BrowserVerdict({ verdict }: { verdict: Verdict }) {
  const hard = verdict.violations.filter((v) => v.severity === "hard").length;
  const strain = verdict.violations.length - hard;
  const status = verdict.playable ? (strain ? "findings" : "passes") : "findings";
  return (
    <div data-testid="browser-verdict" data-status={verdict.playable ? "passes" : "findings"}>
      <p className={`verdict verdict-${status}`}>
        <span className="verdict-icon" aria-hidden="true">{verdict.playable ? "✓" : "!"}</span>
        <strong>{verdict.playable ? "Fits your current hand profile" : `${hard} beyond your current limits`}</strong>
      </p>
      <p className="muted">{`${hard} beyond your limits · ${strain} stretches · checked in the browser with the same rules as the server's quick check, not the full solver.`}</p>
      {verdict.violations.length ? (
        <ul>
          {verdict.violations.slice(0, 20).map((v, i) => <li key={i}>{`Bar ${v.bar ?? "?"}, ${v.hand === "L" ? "left" : v.hand === "R" ? "right" : "both"}: ${v.message}`}</li>)}
          {verdict.violations.length > 20 ? <li>{`${verdict.violations.length - 20} more.`}</li> : null}
        </ul>
      ) : null}
    </div>
  );
}

export function AdvancedPanel({ arrangement, playback, profile, onEvaluate }: AdvancedPanelProps) {
  const [text, setText] = useState(() => (arrangement ? JSON.stringify(arrangement.plan, null, 2) : ""));
  const [error, setError] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const score = useMemo(() => (playback && arrangement ? scoreFromPlayback(playback, `Arrangement ${arrangement.revision}`) : null), [playback, arrangement]);

  if (!arrangement) {
    return <><h2>Advanced</h2><p>Make an arrangement first. Its plan and the engine's diagnostics appear here.</p></>;
  }

  function evaluate() {
    let plan: Record<string, unknown>;
    try { plan = JSON.parse(text) as Record<string, unknown>; } catch { setError("That is not valid JSON."); return; }
    setError("");
    const found = score ? validatePlan(plan as unknown as Plan, score) : [];
    setProblems(found);
    if (found.length) return;
    onEvaluate(plan);
  }

  const pre = (value: unknown) => <pre className="code" tabIndex={0}>{JSON.stringify(value, null, 2)}</pre>;

  return (
    <>
      <h2>Advanced</h2>
      <Field label="Arrangement plan (JSON)" hint="The plan is a list of decisions, not notes. Edit it and evaluate to get a new revision." error={error}>
        <textarea rows={16} spellCheck={false} className="code" data-testid="plan-json" value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
      {problems.length ? (
        <div className="notice" role="alert" data-testid="plan-problems">
          <strong>The plan was not sent. </strong>
          <ul>{problems.map((p) => <li key={p}>{p}</li>)}</ul>
        </div>
      ) : null}
      <div className="toolbar">
        <button type="button" className="button" data-testid="plan-evaluate" onClick={evaluate}>Evaluate this plan</button>
        <button type="button" className="button" data-testid="plan-check" disabled={!score} onClick={() => { if (score) setVerdict(verifyScore(score, handProfile(profile))); }}>Check against my current hands</button>
      </div>
      {verdict ? <BrowserVerdict verdict={verdict} /> : null}
      <details data-testid="json-report"><summary>Engine report</summary>{pre(arrangement.report)}</details>
      <details data-testid="json-verdict"><summary>Verdict</summary>{pre(arrangement.verdict)}</details>
      <details data-testid="json-profile"><summary>Hand profile used</summary>{pre(arrangement.profile)}</details>
    </>
  );
}
