// The browser-side playability check, ported from the original frontend's
// fallback verifier. It is a preview for the plan editor while the server
// has not answered: range, hand span, per-hand and total polyphony, and leap
// feasibility, with a greedy hand assignment per instant. The server's
// `arranger.verify` (a phrase-based search) is the oracle; this never
// overrules it and every result says so.

export interface ScoreNote {
  pitch: number;
  onset: number;
  duration: number;
  staff?: number | null;
  bar?: number | null;
}

export interface Score {
  title?: string;
  tempo_bpm?: number;
  notes: ScoreNote[];
}

export interface HandProfile {
  name?: string;
  lowest_pitch: number;
  highest_pitch: number;
  max_span: number;
  comfortable_span: number;
  max_notes_per_hand: number;
  max_leap_rate: number;
  leap_slack: number;
}

export type Hand = "L" | "R";
export type Severity = "hard" | "strain";
export type Rule = "range" | "hand_span" | "hand_polyphony" | "leap_infeasible" | "total_polyphony";

export interface Violation {
  rule: Rule;
  severity: Severity;
  time: number;
  bar: number | null;
  hand: Hand | null;
  pitches: number[];
  measured: number;
  limit: number;
  message: string;
}

export interface Verdict {
  title: string;
  profile: string;
  playable: boolean;
  n_hard: number;
  n_strain: number;
  violations: Violation[];
  source: "browser";
}

const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const EPS = 0.000001;

export function pitchName(midi: number): string {
  return `${NOTE_NAMES[((midi % 12) + 12) % 12]}${Math.floor(midi / 12) - 1}`;
}

function offset(note: ScoreNote): number {
  return Number(note.onset) + Number(note.duration);
}

function soundsAt(note: ScoreNote, time: number): boolean {
  return Number(note.onset) - EPS <= time && time < offset(note) - EPS;
}

export function onsets(score: Score): number[] {
  return [...new Set(score.notes.map((n) => Number(n.onset)))].sort((a, b) => a - b);
}

export function soundingAt(score: Score, time: number): ScoreNote[] {
  return score.notes.filter((note) => soundsAt(note, time));
}

function centroid(pitches: number[]): number | null {
  if (!pitches.length) return null;
  return pitches.reduce((sum, pitch) => sum + pitch, 0) / pitches.length;
}

type Centroids = [number | null, number | null];
type Assignment = Record<number, Hand>;

function centroidsFor(sounding: ScoreNote[], assignment: Assignment, previous: Centroids): Centroids {
  const left: number[] = [];
  const right: number[] = [];
  sounding.forEach((note, index) => {
    if (assignment[index] === "L") left.push(note.pitch);
    if (assignment[index] === "R") right.push(note.pitch);
  });
  return [centroid(left) ?? previous[0], centroid(right) ?? previous[1]];
}

// Which hand takes which note at one instant. Notes with a staff are decided
// by it; otherwise every split of the notes by pitch is costed (reach, finger
// count, distance from where each hand was, crossing) and the cheapest wins.
export function assignHands(sounding: ScoreNote[], profile: HandProfile, previous: Centroids = [null, null]): [Assignment, Centroids] {
  if (!sounding.length) return [{}, previous];

  if (sounding.some((note) => note.staff !== undefined && note.staff !== null)) {
    const assignment: Assignment = {};
    sounding.forEach((note, index) => {
      assignment[index] = (note.staff || 1) >= 2 ? "L" : "R";
    });
    return [assignment, centroidsFor(sounding, assignment, previous)];
  }

  const order = sounding
    .map((note, index) => [note.pitch, index] as const)
    .sort((a, b) => a[0] - b[0])
    .map((item) => item[1]);
  let best: { cost: number; assignment: Assignment } | null = null;

  for (let split = 0; split <= order.length; split += 1) {
    const leftIndexes = order.slice(0, split);
    const rightIndexes = order.slice(split);
    const leftPitches = leftIndexes.map((index) => sounding[index].pitch);
    const rightPitches = rightIndexes.map((index) => sounding[index].pitch);
    let cost = 0;

    for (const pitches of [leftPitches, rightPitches]) {
      if (!pitches.length) continue;
      const span = Math.max(...pitches) - Math.min(...pitches);
      if (span > profile.max_span) cost += 1000 * (span - profile.max_span);
      else if (span > profile.comfortable_span) cost += 5 * (span - profile.comfortable_span);
      if (pitches.length > profile.max_notes_per_hand) cost += 1000 * (pitches.length - profile.max_notes_per_hand);
    }

    const leftCentroid = centroid(leftPitches);
    const rightCentroid = centroid(rightPitches);
    for (const [current, prev] of [[leftCentroid, previous[0]], [rightCentroid, previous[1]]] as const) {
      if (current === null) continue;
      cost += prev === null ? 24 : Math.abs(current - prev);
    }
    if (leftCentroid !== null && rightCentroid !== null && leftCentroid > rightCentroid) cost += 500;

    const assignment: Assignment = {};
    for (const index of leftIndexes) assignment[index] = "L";
    for (const index of rightIndexes) assignment[index] = "R";
    if (!best || cost < best.cost) best = { cost, assignment };
  }

  return [best!.assignment, centroidsFor(sounding, best!.assignment, previous)];
}

function violation(fields: Partial<Violation> & Pick<Violation, "rule" | "severity" | "time" | "message">): Violation {
  return { bar: null, hand: null, pitches: [], measured: 0, limit: 0, ...fields };
}

function barOf(sounding: ScoreNote[]): number | null {
  return sounding.find((note) => note.bar !== undefined && note.bar !== null)?.bar ?? null;
}

export function verifyScore(score: Score, profile: HandProfile): Verdict {
  const violations: Violation[] = [];

  for (const note of score.notes) {
    if (note.pitch < profile.lowest_pitch || note.pitch > profile.highest_pitch) {
      violations.push(violation({
        rule: "range", severity: "hard", time: note.onset, bar: note.bar ?? null, pitches: [note.pitch],
        measured: note.pitch,
        limit: note.pitch < profile.lowest_pitch ? profile.lowest_pitch : profile.highest_pitch,
        message: `${pitchName(note.pitch)} is outside ${pitchName(profile.lowest_pitch)}-${pitchName(profile.highest_pitch)}`,
      }));
    }
  }

  let previousCentroids: Centroids = [null, null];
  const lastPlayed: [number | null, number | null] = [null, null];

  for (const time of onsets(score)) {
    const sounding = soundingAt(score, time);
    if (!sounding.length) continue;
    const [assignment, centroids] = assignHands(sounding, profile, previousCentroids);
    const bar = barOf(sounding);
    const active = new Set(Object.values(assignment));

    for (const hand of ["L", "R"] as const) {
      const pitches = sounding.filter((_, index) => assignment[index] === hand).map((note) => note.pitch).sort((a, b) => a - b);
      if (!pitches.length) continue;
      const span = pitches[pitches.length - 1] - pitches[0];
      if (span > profile.max_span) {
        violations.push(violation({
          rule: "hand_span", severity: "hard", time, bar, hand, pitches, measured: span, limit: profile.max_span,
          message: `${hand}H must span ${span} semitones (${pitchName(pitches[0])}-${pitchName(pitches[pitches.length - 1])}).`,
        }));
      } else if (span > profile.comfortable_span) {
        violations.push(violation({
          rule: "hand_span", severity: "strain", time, bar, hand, pitches, measured: span, limit: profile.comfortable_span,
          message: `${hand}H stretch of ${span} semitones is reachable but tiring.`,
        }));
      }
      if (pitches.length > profile.max_notes_per_hand) {
        violations.push(violation({
          rule: "hand_polyphony", severity: "hard", time, bar, hand, pitches, measured: pitches.length,
          limit: profile.max_notes_per_hand, message: `${hand}H needs ${pitches.length} fingers.`,
        }));
      }
    }

    (["L", "R"] as const).forEach((hand, index) => {
      if (!active.has(hand)) return;
      const since = lastPlayed[index];
      const before = previousCentroids[index];
      const after = centroids[index];
      lastPlayed[index] = time;
      if (before === null || after === null || since === null) return;
      const dt = time - since;
      const displacement = Math.abs(after - before);
      const budget = profile.leap_slack + profile.max_leap_rate * dt;
      if (displacement > budget) {
        violations.push(violation({
          rule: "leap_infeasible", severity: "hard", time, bar, hand, measured: displacement,
          limit: Number(budget.toFixed(2)),
          message: `${hand}H moves ${displacement.toFixed(0)} semitones in ${(dt * 1000).toFixed(0)}ms.`,
        }));
      }
    });

    previousCentroids = centroids;
  }

  const totalLimit = profile.max_notes_per_hand * 2;
  for (const time of onsets(score)) {
    const sounding = soundingAt(score, time);
    if (sounding.length > totalLimit) {
      violations.push(violation({
        rule: "total_polyphony", severity: "hard", time, bar: barOf(sounding),
        pitches: sounding.map((note) => note.pitch).sort((a, b) => a - b), measured: sounding.length, limit: totalLimit,
        message: `${sounding.length} notes sounding at once; only ${totalLimit} fingers.`,
      }));
    }
  }

  violations.sort((a, b) => a.time - b.time || a.rule.localeCompare(b.rule));
  const hard = violations.filter((v) => v.severity === "hard");
  return {
    title: score.title || "untitled",
    profile: profile.name || "custom",
    playable: hard.length === 0,
    n_hard: hard.length,
    n_strain: violations.filter((v) => v.severity === "strain").length,
    violations,
    source: "browser",
  };
}

// --- plans -------------------------------------------------------------------

export interface PlanSection {
  start_bar: number;
  end_bar: number;
  lh_pattern: string;
  lh_voices?: number;
  melody_shift?: number;
  lh_octave?: number;
  melody_fold_window?: number;
  [key: string]: unknown;
}

export interface Plan {
  sections: PlanSection[];
  [key: string]: unknown;
}

export const LH_PATTERNS = ["block", "pedal_tone", "broken_octave", "arpeggio", "alberti", "walking", "stride", "broken_tenth"] as const;

export function lastBar(score: Score): number {
  return Math.max(1, ...score.notes.map((note) => note.bar ?? 1));
}

// The checks the server's `ArrangementPlan.validate` makes first, so the plan
// editor can say what is wrong before a request is sent.
export function validatePlan(plan: Plan, score: Score): string[] {
  const problems: string[] = [];
  if (!Array.isArray(plan.sections) || !plan.sections.length) return ["A plan needs at least one section."];
  const end = lastBar(score);
  const sorted = [...plan.sections].sort((a, b) => a.start_bar - b.start_bar);
  sorted.forEach((section, index) => {
    const where = `Section ${index + 1}`;
    if (!Number.isInteger(section.start_bar) || !Number.isInteger(section.end_bar)) problems.push(`${where}: bars must be whole numbers.`);
    else if (section.start_bar < 1) problems.push(`${where}: bars are numbered from 1.`);
    else if (section.start_bar > section.end_bar) problems.push(`${where}: start_bar is after end_bar.`);
    else if (section.end_bar > end) problems.push(`${where}: ends at bar ${section.end_bar}, the piece has ${end}.`);
    if (!LH_PATTERNS.includes(section.lh_pattern as (typeof LH_PATTERNS)[number])) {
      problems.push(`${where}: '${section.lh_pattern}' is not a left-hand pattern (${LH_PATTERNS.join(", ")}).`);
    }
    if (section.lh_voices !== undefined && (!Number.isInteger(section.lh_voices) || section.lh_voices < 0 || section.lh_voices > 5)) {
      problems.push(`${where}: lh_voices must be 0 to 5.`);
    }
    if (section.melody_fold_window !== undefined && section.melody_fold_window !== 0 && (section.melody_fold_window < 7 || section.melody_fold_window > 24)) {
      problems.push(`${where}: melody_fold_window must be 0 or 7 to 24.`);
    }
    const next = sorted[index + 1];
    if (next && next.start_bar <= section.end_bar) problems.push(`${where} and section ${index + 2} both cover bar ${next.start_bar}.`);
  });
  return problems;
}
