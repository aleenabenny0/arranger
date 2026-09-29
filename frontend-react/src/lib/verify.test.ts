import { describe, expect, it } from "vitest";
import { assignHands, lastBar, pitchName, validatePlan, verifyScore, type HandProfile, type Score } from "./verify";

const profile: HandProfile = {
  name: "test", lowest_pitch: 21, highest_pitch: 108, max_span: 12, comfortable_span: 9,
  max_notes_per_hand: 5, max_leap_rate: 70, leap_slack: 5,
};

function score(notes: Score["notes"], title = "t"): Score {
  return { title, tempo_bpm: 100, notes };
}

describe("verifyScore", () => {
  it("passes a comfortable chord and reports the hands it chose", () => {
    const verdict = verifyScore(score([{ pitch: 60, onset: 0, duration: 1 }, { pitch: 64, onset: 0, duration: 1 }]), profile);
    expect(verdict.playable).toBe(true);
    expect(verdict.violations).toEqual([]);
    expect(verdict.source).toBe("browser");
    expect(verdict.n_hard + verdict.n_strain).toBe(0);
  });

  it("flags a note outside the keyboard", () => {
    const verdict = verifyScore(score([{ pitch: 12, onset: 0, duration: 1, bar: 3 }]), profile);
    expect(verdict.playable).toBe(false);
    expect(verdict.violations[0]).toMatchObject({ rule: "range", severity: "hard", bar: 3, measured: 12, limit: 21 });
    expect(verdict.violations[0].message).toContain("C0");
  });

  it("splits a wide spread across two hands before calling it a reach problem", () => {
    // C3 and C5: 24 semitones, one per hand is fine.
    expect(verifyScore(score([{ pitch: 48, onset: 0, duration: 1 }, { pitch: 72, onset: 0, duration: 1 }]), profile).playable).toBe(true);
    // Forced into one hand by the staff, it is not.
    const forced = verifyScore(score([{ pitch: 48, onset: 0, duration: 1, staff: 2 }, { pitch: 69, onset: 0, duration: 1, staff: 2 }]), profile);
    expect(forced.violations.map((v) => v.rule)).toEqual(["hand_span"]);
    expect(forced.violations[0]).toMatchObject({ hand: "L", measured: 21, limit: 12, severity: "hard" });
  });

  it("reports a stretch as strain and too many fingers as hard", () => {
    const stretch = verifyScore(score([{ pitch: 60, onset: 0, duration: 1, staff: 1 }, { pitch: 71, onset: 0, duration: 1, staff: 1 }]), profile);
    expect(stretch.playable).toBe(true);
    expect(stretch.violations[0]).toMatchObject({ rule: "hand_span", severity: "strain", measured: 11, limit: 9 });
    const six = verifyScore(score([60, 62, 64, 65, 67, 69].map((pitch) => ({ pitch, onset: 0, duration: 1, staff: 1 }))), profile);
    expect(six.violations.some((v) => v.rule === "hand_polyphony" && v.measured === 6 && v.limit === 5)).toBe(true);
  });

  it("refuses a leap the hand cannot make in time and allows one it can", () => {
    const fast = verifyScore(score([{ pitch: 36, onset: 0, duration: 0.02, staff: 2 }, { pitch: 84, onset: 0.02, duration: 0.5, staff: 2 }]), profile);
    expect(fast.violations.map((v) => v.rule)).toContain("leap_infeasible");
    const slow = verifyScore(score([{ pitch: 36, onset: 0, duration: 0.5, staff: 2 }, { pitch: 84, onset: 2, duration: 0.5, staff: 2 }]), profile);
    expect(slow.playable).toBe(true);
  });

  it("counts every finger on the keyboard", () => {
    const eleven = verifyScore(score(Array.from({ length: 11 }, (_, i) => ({ pitch: 40 + 3 * i, onset: 0, duration: 1 }))), profile);
    expect(eleven.violations.some((v) => v.rule === "total_polyphony" && v.measured === 11 && v.limit === 10)).toBe(true);
  });

  it("sorts findings by time then rule", () => {
    const verdict = verifyScore(score([
      { pitch: 12, onset: 1, duration: 1 },
      { pitch: 48, onset: 0, duration: 1, staff: 2 }, { pitch: 69, onset: 0, duration: 1, staff: 2 },
    ]), profile);
    expect(verdict.violations.map((v) => [v.time, v.rule])).toEqual([[0, "hand_span"], [1, "range"]]);
  });
});

describe("assignHands", () => {
  it("keeps a low note in the left hand and a high one in the right", () => {
    const [assignment] = assignHands([{ pitch: 40, onset: 0, duration: 1 }, { pitch: 80, onset: 0, duration: 1 }], profile);
    expect(assignment).toEqual({ 0: "L", 1: "R" });
  });
});

describe("pitchName", () => {
  it("names middle C and the ends of the keyboard", () => {
    expect(pitchName(60)).toBe("C4");
    expect(pitchName(21)).toBe("A0");
    expect(pitchName(108)).toBe("C8");
  });
});

describe("validatePlan", () => {
  const piece = score([{ pitch: 60, onset: 0, duration: 1, bar: 1 }, { pitch: 62, onset: 4, duration: 1, bar: 8 }]);

  it("accepts a plan that covers the piece with known patterns", () => {
    expect(lastBar(piece)).toBe(8);
    expect(validatePlan({ sections: [{ start_bar: 1, end_bar: 8, lh_pattern: "block", lh_voices: 2 }] }, piece)).toEqual([]);
  });

  it("names every problem", () => {
    const problems = validatePlan({ sections: [
      { start_bar: 1, end_bar: 12, lh_pattern: "waltz", lh_voices: 9, melody_fold_window: 3 },
      { start_bar: 4, end_bar: 2, lh_pattern: "block" },
    ] }, piece);
    expect(problems).toEqual([
      "Section 1: ends at bar 12, the piece has 8.",
      "Section 1: 'waltz' is not a left-hand pattern (block, pedal_tone, broken_octave, arpeggio, alberti, walking, stride, broken_tenth).",
      "Section 1: lh_voices must be 0 to 5.",
      "Section 1: melody_fold_window must be 0 or 7 to 24.",
      "Section 1 and section 2 both cover bar 4.",
      "Section 2: start_bar is after end_bar.",
    ]);
    expect(validatePlan({ sections: [] }, piece)).toEqual(["A plan needs at least one section."]);
  });
});
