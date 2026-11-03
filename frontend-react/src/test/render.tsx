// Renders a view inside the providers the app uses, with a session already
// resolved so views do not wait for /catalog and /auth/me.
import { render, type RenderResult } from "@testing-library/react";
import type { ReactNode } from "react";
import type { Catalog, User } from "../api/types";
import { SessionProvider } from "../lib/session";
import { ToastProvider } from "../lib/toast";

export const CATALOG: Catalog = {
  presets: [
    { id: "small_hands", description: "Small hands, up to an octave.", profile: { max_span: 10, comfortable_span: 8, max_notes_per_hand: 4, max_leap_rate: 60, skill_level: 4, lowest_pitch: 21, highest_pitch: 108, left_fingers: [1, 2, 3, 4, 5], right_fingers: [1, 2, 3, 4, 5], leap_slack: 5 } },
    { id: "intermediate", description: "Most adult learners.", profile: { max_span: 12, comfortable_span: 10, max_notes_per_hand: 4, max_leap_rate: 70, skill_level: 5, lowest_pitch: 21, highest_pitch: 108, left_fingers: [1, 2, 3, 4, 5], right_fingers: [1, 2, 3, 4, 5], leap_slack: 5 } },
    { id: "advanced", description: "Wide reach, fast hands.", profile: { max_span: 14, comfortable_span: 12, max_notes_per_hand: 5, max_leap_rate: 120, skill_level: 8, lowest_pitch: 21, highest_pitch: 108, left_fingers: [1, 2, 3, 4, 5], right_fingers: [1, 2, 3, 4, 5], leap_slack: 5 } },
  ],
  capabilities: { export_pdf: { available: false, reason: "LilyPond is not installed." }, import_audio: { available: false }, model_repair: { available: false } },
  calibration_steps: [
    { field: "left_max_white_keys", ask: "Left hand: how many white keys can you span?", hint: "Thumb to little finger." },
    { field: "right_max_white_keys", ask: "Right hand: how many white keys can you span?" },
  ],
  limits: { max_upload_bytes: 40 * 1024 * 1024 },
};

export const USER: User = { id: "u1", email: "pianist@example.com", display_name: "pianist", email_verified: true };

export function renderView(ui: ReactNode, { user = USER, catalog = CATALOG }: { user?: User | null; catalog?: Catalog | null } = {}): RenderResult {
  return render(
    <SessionProvider initial={{ ready: true, user, catalog }}>
      <ToastProvider>{ui}</ToastProvider>
    </SessionProvider>,
  );
}
