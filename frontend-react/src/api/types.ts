// Shapes of the responses the OpenAPI document leaves as free-form objects
// (the API returns dicts for these). Request bodies and the auth responses are
// typed by the generated schema; these cover what the views read.
import type { components } from "./schema";

export type ArrangeIn = components["schemas"]["ArrangeIn"];
export type PlayerProfileIn = components["schemas"]["PlayerProfileIn"];

// The auth responses type `user` as a free-form object; this is what
// arranger_api.auth_routes.user_payload sends.
export interface User {
  id: string;
  email: string;
  display_name: string;
  email_verified: boolean;
  created_at?: string;
}

export interface SessionRow {
  id: string;
  current: boolean;
  user_agent?: string | null;
  ip?: string | null;
  ip_address?: string | null;
  created_at: string;
  last_seen_at?: string | null;
}

export interface Capability { available: boolean; reason?: string | null }

export interface Preset { id: string; description: string; profile: Record<string, unknown> }

export interface CalibrationStep { field: string; ask: string; hint?: string }

export interface Catalog {
  presets: Preset[];
  capabilities: Record<string, Capability>;
  calibration_steps: CalibrationStep[];
  limits: { max_upload_bytes: number; [key: string]: unknown };
  [key: string]: unknown;
}

export interface ProjectRow {
  id: string;
  title: string;
  composer: string | null;
  source_kind: string;
  bar_count: number | null;
  arrangement_count: number;
  updated_at: string;
  current_arrangement_id?: string | null;
  current_source_id?: string | null;
  profile?: Record<string, unknown> | null;
}

export interface ProjectList { projects: ProjectRow[]; total: number; limit: number; offset: number }

export interface Track {
  index: number;
  name: string | null;
  note_count: number;
  lowest_pitch: number | null;
  highest_pitch: number | null;
  melody_likelihood: number | null;
}

export interface Inspection {
  bar_count: number;
  duration_seconds: number;
  note_count: number;
  key: { fifths: number; mode: string; estimated?: boolean } | null;
  meter: [number, number] | null;
  meter_changes?: number;
  tempo_bpm: number;
  tempo_changes?: number;
  difficulty?: { level: number; label: string } | null;
  as_written?: { without_pedal: { headline: string }; with_pedal_each_bar: { headline: string } } | null;
  transcription?: { note: string; overall_confidence: number } | null;
  confidence?: { note: string; low_confidence_note_ids: string[] } | null;
  warnings?: string[];
  tracks?: Track[];
  suggested_melody_track?: number | null;
}

export interface Selection {
  melody_track?: number | null;
  ignored_tracks?: number[];
  transpose?: number;
  tempo_bpm?: number | null;
  meter?: [number, number] | null;
  edits?: { op: string; note_id: string }[];
}

export interface Playback {
  notes: [number, number, number, number, number, string, number][];   // pitch, onset, duration, velocity, staff, id, bar
  duration: number;
  bars: [number, number][];                                            // bar number, start in seconds
}

export interface Source {
  id: string;
  revision: number;
  kind: string;
  filename: string | null;
  inspection: Inspection;
  selection: Selection;
  playback?: Playback;
}

export interface JobRow { id: string; kind: string; status: string }

export interface Finding {
  rule: string;
  severity: "hard" | "strain";
  bar: number | null;
  time: number;
  hand: "L" | "R" | "both" | null;
  message: string;
  certainty?: string;
  note_ids?: string[];
  measured?: number;
  limit?: number;
}

export interface FindingsSummary { status: "passes" | "findings" | "unresolved"; headline: string; detail: string; hard: number; strain: number }

export interface Summary {
  findings: FindingsSummary;
  fidelity: Record<string, number>;
  difficulty: { level: number; label: string; note: string };
  tempo_scale: number;
  tempo_bpm: number;
}

export interface ArrangementRow {
  id: string;
  revision: number;
  label: string | null;
  created_at: string;
  accepted: boolean;
  n_hard: number;
  fidelity_score: number;
  difficulty: number;
  source_id: string;
}

export interface Arrangement extends ArrangementRow {
  algorithm_version: string;
  model: string | null;
  summary: Summary;
  verdict: { playable: boolean; violations: Finding[]; violations_truncated?: number; [key: string]: unknown };
  report: { sentences?: string[]; [key: string]: unknown };
  plan: Record<string, unknown>;
  profile: Record<string, unknown>;
}

export interface ProjectDetail {
  project: ProjectRow & { current_arrangement_id: string | null; current_source_id: string | null; profile: Record<string, unknown> | null };
  source: Source | null;
  sources: { id: string; revision: number }[];
  arrangements: ArrangementRow[];
  jobs: JobRow[];
}

export interface ArrangementDetail { arrangement: Arrangement; playback: Playback }

export interface ComparisonSide { revision: number; accepted: boolean; hard: number; strain: number; fidelity: number; difficulty: number }

export interface Comparison { a: ComparisonSide; b: ComparisonSide; profile_changes: string[]; plan_changes: string[]; same_source: boolean }

export interface Usage {
  projects: { used: number; limit: number };
  storage_bytes: { used: number; limit: number };
  jobs_today: { used: number; limit: number };
  retention: { upload_days: number; export_days: number };
}
