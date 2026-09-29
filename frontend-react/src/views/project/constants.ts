export const RULE_NAMES: Record<string, string> = {
  range: "Out of range", hand_span: "Reach", hand_polyphony: "Too many notes in one hand", leap_infeasible: "Leap too fast",
  total_polyphony: "Too many notes at once", legato_break: "Note must be released early", hand_crossing: "Hands cross",
  fast_repetition: "Fast repeated note", finger_stretch: "Awkward between fingers",
};

export const FIDELITY_LABELS: [string, string][] = [
  ["melodic_recall", "Melody kept"], ["rhythm", "Rhythm kept"], ["contour", "Shape of the tune"],
  ["harmonic_coverage", "Harmony kept"], ["bass", "Bass line kept"], ["accompaniment", "Left hand kept busy"],
];

export interface ProfileField { key: string; label: string; hint: string; min: number; max: number }

export const PROFILE_FIELDS: ProfileField[] = [
  { key: "max_span", label: "Largest reach (semitones)", hint: "12 is an octave, 14 a ninth, 16 a tenth.", min: 1, max: 24 },
  { key: "comfortable_span", label: "Comfortable reach (semitones)", hint: "Wider than this is allowed but marked as a stretch.", min: 1, max: 24 },
  { key: "max_notes_per_hand", label: "Notes one hand can hold at once", hint: "", min: 1, max: 5 },
  { key: "max_leap_rate", label: "Hand travel speed (semitones per second)", hint: "70 suits most intermediate players.", min: 5, max: 400 },
  { key: "skill_level", label: "Level, 1 to 10", hint: "Decides which left-hand figures are offered.", min: 1, max: 10 },
  { key: "lowest_pitch", label: "Lowest key (MIDI number)", hint: "21 is the bottom A of a full piano.", min: 0, max: 127 },
  { key: "highest_pitch", label: "Highest key (MIDI number)", hint: "108 is the top C of a full piano.", min: 0, max: 127 },
];

export type Profile = Record<string, unknown>;

export const FINGERS = [1, 2, 3, 4, 5];

export function fingersOf(profile: Profile, key: string): number[] {
  const value = profile[key];
  return Array.isArray(value) ? (value as number[]) : FINGERS;
}
