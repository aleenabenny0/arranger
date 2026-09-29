import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Arrangement, ArrangementDetail, Playback, ProjectDetail } from "../../api/types";
import { apiError, installApi, jsonResponse, textResponse, type FakeApi } from "../../test/fakeApi";
import { CATALOG, renderView } from "../../test/render";
import { handProfile, scoreFromPlayback } from "./AdvancedPanel";
import { comparisonLine } from "./RevisionsPanel";
import { ProjectView, staleReasons } from "./ProjectView";

const PLAYBACK: Playback = {
  notes: [[72, 0, 0.5, 80, 1, "n1", 1], [48, 0, 0.5, 70, 2, "n2", 1], [74, 0.5, 0.5, 80, 1, "n3", 1], [76, 1, 0.5, 80, 1, "n4", 2]],
  duration: 2,
  bars: [[1, 0], [2, 1]],
};

const SOURCE = {
  id: "s1", revision: 1, kind: "midi", filename: "study.mid", selection: {},
  inspection: {
    bar_count: 4, duration_seconds: 8, note_count: 32, key: { fifths: 0, mode: "major" }, meter: [4, 4] as [number, number], tempo_bpm: 120,
    difficulty: { level: 3, label: "easy" }, warnings: ["Pedal marks were dropped."],
    tracks: [
      { index: 0, name: "Tune", note_count: 16, lowest_pitch: 72, highest_pitch: 79, melody_likelihood: 0.9 },
      { index: 1, name: null, note_count: 16, lowest_pitch: 48, highest_pitch: 55, melody_likelihood: 0.1 },
    ],
    suggested_melody_track: 0,
  },
};

const ARRANGEMENT: Arrangement = {
  id: "a1", revision: 1, label: null, created_at: "2026-09-29T10:00:00Z", accepted: false, n_hard: 1, fidelity_score: 0.92, difficulty: 3, source_id: "s1",
  algorithm_version: "engine-1", model: null,
  summary: {
    findings: { status: "findings", headline: "One reach is beyond your limits", detail: "Bar 2 needs a tenth.", hard: 1, strain: 0 },
    fidelity: { score: 0.92, melodic_recall: 1, rhythm: 0.9, contour: 0.95, harmonic_coverage: 0.8, bass: 0.7, accompaniment: 0.6 },
    difficulty: { level: 3, label: "easy", note: "Mostly single notes." }, tempo_scale: 0.8, tempo_bpm: 96,
  },
  verdict: { playable: false, violations: [{ rule: "hand_span", severity: "hard", bar: 2, time: 1, hand: "L", message: "Reach of 16 semitones.", note_ids: ["n4"] }] },
  report: { sentences: ["The tune was kept in full.", "The left hand plays a pedal tone."] },
  plan: { sections: [{ start_bar: 1, end_bar: 2, lh_pattern: "block", lh_voices: 2 }] },
  profile: { max_span: 12 },
};

function project(overrides: Partial<ProjectDetail["project"]> = {}, arrangements: ProjectDetail["arrangements"] = []): ProjectDetail {
  return {
    project: { id: "p1", title: "Study", composer: "Anon", source_kind: "midi", bar_count: 4, arrangement_count: arrangements.length, updated_at: "2026-09-29T10:00:00Z",
      current_arrangement_id: null, current_source_id: "s1", profile: null, ...overrides },
    source: SOURCE, sources: [{ id: "s1", revision: 1 }], arrangements, jobs: [],
  };
}

const DETAIL: ArrangementDetail = { arrangement: ARRANGEMENT, playback: PLAYBACK };
const ROW = { id: "a1", revision: 1, label: null, created_at: ARRANGEMENT.created_at, accepted: false, n_hard: 1, fidelity_score: 0.92, difficulty: 3, source_id: "s1" };

let api: FakeApi;

beforeEach(() => {
  api = installApi();
  window.location.hash = "/project/p1";
  api.on("GET", "/projects/p1/sources/s1", { source: SOURCE, playback: PLAYBACK });
  api.on("GET", "/projects/p1/arrangements/a1", DETAIL);
  api.on("GET", "/projects/p1/arrangements/a1/musicxml", textResponse(200, "<score-partwise/>", "application/vnd.recordare.musicxml+xml"));
  api.on("GET", "/projects/p1/sources/s1/musicxml", textResponse(200, "<score-partwise/>", "application/vnd.recordare.musicxml+xml"));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ProjectView", () => {
  it("shows the source facts, the corrections form and saves corrections as a revision", async () => {
    let detail = project();
    api.on("GET", "/projects/p1", () => jsonResponse(200, detail));
    api.on("POST", "/projects/p1/sources", () => { detail = { ...detail, source: { ...SOURCE, id: "s2", revision: 2 }, project: { ...detail.project, current_source_id: "s2" } }; return jsonResponse(201, { source: detail.source }); });
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("project-title")).toHaveTextContent("Study"));
    expect(screen.getByTestId("tab-source")).toHaveAttribute("aria-selected", "true");
    const facts = document.querySelector("dl.facts")!;
    expect(facts).toHaveTextContent("4 bars, 0:08");
    expect(facts).toHaveTextContent("C major");
    expect(facts).toHaveTextContent("120 beats per minute");
    expect(screen.getByText("Likely the tune")).toBeInTheDocument();
    expect(screen.getByText("Pedal marks were dropped.")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Use part 2 as the melody"));
    await user.click(screen.getByLabelText("Leave out Tune"));
    await user.selectOptions(screen.getByLabelText("Transpose the whole piece"), "-2");
    await user.click(screen.getByRole("button", { name: "Save corrections" }));
    await waitFor(() => expect(api.sent("POST", "/projects/p1/sources")).toHaveLength(1));
    expect(api.sent("POST", "/projects/p1/sources")[0].body).toEqual({ based_on: "s1", selection: { melody_track: 1, ignored_tracks: [0], transpose: -2, edits: [] } });
    await waitFor(() => expect(screen.getByText("Anon · source revision 2")).toBeInTheDocument());
    expect(screen.getByTestId("toast-success")).toHaveTextContent("Corrections saved");
  });

  it("edits the hand profile from a preset or by hand, refusing values out of range", async () => {
    api.on("GET", "/projects/p1", project());
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("project-title")).toBeInTheDocument());
    await user.click(screen.getByTestId("tab-hands"));
    const reach = screen.getByTestId("profile-max_span");
    expect(reach).toHaveValue(12);   // the intermediate preset when the project has none
    await user.selectOptions(screen.getByTestId("profile-preset"), "small_hands");
    expect(reach).toHaveValue(10);
    expect(screen.getByText("Small hands, up to an octave.")).toBeInTheDocument();
    await user.clear(reach);
    await user.type(reach, "11");
    expect(screen.getByTestId("profile-preset")).toHaveValue("");
    expect(reach).toHaveValue(11);
    await user.clear(reach);
    await user.type(reach, "99");
    expect(screen.getByText("Enter a number from 1 to 24.")).toBeInTheDocument();
    expect(reach).toHaveValue(99);
    // Fingers: at least one must stay.
    for (const finger of [1, 2, 3, 4]) await user.click(screen.getByLabelText(finger === 1 ? "1 (thumb)" : String(finger), { selector: `#left_fingers-${finger}` }));
    await user.click(document.getElementById("left_fingers-5")!);
    expect(document.getElementById("left_fingers-5")).toBeChecked();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Keep at least one finger."));
  });

  it("measures the hands step by step", async () => {
    api.on("GET", "/projects/p1", project());
    api.on("POST", "/catalog/calibrate", { profile: { ...CATALOG.presets[1].profile, max_span: 13 } });
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("project-title")).toBeInTheDocument());
    await user.click(screen.getByTestId("tab-hands"));
    await user.click(screen.getByRole("button", { name: "Measure my hands step by step" }));
    const dialog = screen.getByRole("dialog", { name: "Measure your hands" });
    expect(dialog).toHaveTextContent("Question 1 of 2");
    await user.click(within(dialog).getByRole("button", { name: "Next" }));
    expect(within(dialog).getByText("Enter a number, or skip this one.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Your answer"), "8");
    await user.click(within(dialog).getByRole("button", { name: "Next" }));
    expect(dialog).toHaveTextContent("Question 2 of 2");
    await user.click(within(dialog).getByRole("button", { name: "Skip this one" }));
    await waitFor(() => expect(api.sent("POST", "/catalog/calibrate")).toHaveLength(1));
    expect(api.sent("POST", "/catalog/calibrate")[0].body).toEqual({ base_preset: "intermediate", name: "My hands", left_max_white_keys: 8 });
    await waitFor(() => expect(screen.getByTestId("profile-max_span")).toHaveValue(13));
    expect(screen.getByTestId("toast-success")).toHaveTextContent("updated from your measurements");
  });

  it("arranges through the job resource and shows the result, findings and plan", async () => {
    let detail = project();
    api.on("GET", "/projects/p1", () => jsonResponse(200, detail));
    api.on("POST", "/jobs/arrange", (call) => {
      detail = project({ current_arrangement_id: "a1", profile: (call.body as { profile: Record<string, unknown> }).profile }, [ROW]);
      return jsonResponse(202, { job: { id: "j1", status: "queued" } });
    });
    api.on("GET", "/jobs/j1", { job: { id: "j1", status: "succeeded", progress: 1, stage: "done", result: { arrangement_id: "a1" }, error: null } });
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("project-title")).toBeInTheDocument());
    await user.click(screen.getByTestId("tab-advanced"));
    expect(screen.getByTestId("panel-advanced")).toHaveTextContent("Make an arrangement first.");
    await user.click(screen.getByTestId("tab-arrange"));
    await user.click(screen.getByTestId("arrange-button"));
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Arrangement 1" })).toBeInTheDocument());
    const posted = api.sent("POST", "/jobs/arrange")[0];
    expect(posted.headers.get("idempotency-key")).toMatch(/^[0-9a-f]{32}$/);
    expect(posted.body).toMatchObject({ project_id: "p1", use_model: false, profile: { max_span: 12 } });
    expect(screen.getByTestId("verdict")).toHaveAttribute("data-status", "findings");
    expect(screen.getByTestId("fidelity-score")).toHaveTextContent("Fidelity score 92%");
    expect(screen.getByRole("heading", { name: "What was changed" })).toBeInTheDocument();
    expect(screen.getByText("The tune was kept in full.")).toBeInTheDocument();
    expect(screen.getByText(/Suggested tempo: 96 beats per minute \(80% of the original\)/)).toBeInTheDocument();
    const findings = screen.getByTestId("findings");
    expect(within(findings).getByText("Beyond your limits")).toBeInTheDocument();
    expect(within(findings).getByText("Reach")).toBeInTheDocument();
    expect(within(findings).getByRole("button", { name: "Show bar 2 in the score" })).toBeInTheDocument();
    expect(screen.getByTestId("toast-success")).toHaveTextContent("Arrangement ready.");
    expect(screen.getByText("PDF engraving is not installed on this server.")).toBeInTheDocument();
    expect(screen.getByTestId("download-midi")).toBeInTheDocument();
    // The timeline marks bar 2 as beyond the limits.
    expect(within(screen.getByTestId("timeline")).getByTestId("bar-2")).toHaveClass("bar-tile-hard");
    // The plan, verdict and report are there to read and edit.
    await user.click(screen.getByTestId("tab-advanced"));
    expect((screen.getByTestId("plan-json") as HTMLTextAreaElement).value).toMatch(/"sections"/);
    expect(within(screen.getByTestId("json-verdict")).getByText(/"playable": false/)).toBeInTheDocument();
    expect(within(screen.getByTestId("json-report")).getByText(/The tune was kept in full/)).toBeInTheDocument();
    // Revisions list the arrangement.
    await user.click(screen.getByTestId("tab-revisions"));
    const rows = within(screen.getByTestId("revisions")).getAllByRole("row");
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveTextContent("1 beyond limits");
    expect(rows[1]).toHaveTextContent("92%");
  });

  it("marks an arrangement as out of date when the hands change", async () => {
    api.on("GET", "/projects/p1", project({ current_arrangement_id: "a1", profile: { ...CATALOG.presets[0].profile } }, [ROW]));
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Arrangement 1" })).toBeInTheDocument());
    expect(screen.getByTestId("tab-arrange")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("stale-notice")).not.toBeVisible();
    await user.click(screen.getByTestId("tab-hands"));
    await user.selectOptions(screen.getByTestId("profile-preset"), "advanced");
    await user.click(screen.getByTestId("tab-arrange"));
    expect(screen.getByTestId("stale-notice")).toBeVisible();
    expect(screen.getByTestId("stale-notice")).toHaveTextContent("your hand profile has changed since");
    expect(staleReasons(project({ current_source_id: "s2" }), "s1", {}, null)).toEqual(["the source has been corrected since"]);
    expect(staleReasons(null, "s1", {}, null)).toEqual([]);
  });

  it("evaluates an edited plan as a new revision, refusing bad JSON and bad plans first", async () => {
    api.on("GET", "/projects/p1", project({ current_arrangement_id: "a1" }, [ROW]));
    api.on("POST", "/jobs/arrange", jsonResponse(202, { job: { id: "j2" } }));
    api.on("GET", "/jobs/j2", { job: { id: "j2", status: "succeeded", progress: 1, stage: "done", result: { arrangement_id: "a1" }, error: null } });
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Arrangement 1" })).toBeInTheDocument());
    await user.click(screen.getByTestId("tab-advanced"));
    const editor = screen.getByTestId("plan-json");
    await user.clear(editor);
    await user.type(editor, "{{ not json");
    await user.click(screen.getByTestId("plan-evaluate"));
    expect(screen.getByText("That is not valid JSON.")).toBeInTheDocument();
    expect(api.sent("POST", "/jobs/arrange")).toHaveLength(0);
    // A plan that covers bars the piece does not have is refused in the browser.
    await user.clear(editor);
    await user.paste(JSON.stringify({ sections: [{ start_bar: 1, end_bar: 9, lh_pattern: "waltz" }] }));
    await user.click(screen.getByTestId("plan-evaluate"));
    expect(screen.getByTestId("plan-problems")).toHaveTextContent("ends at bar 9, the piece has 2");
    expect(screen.getByTestId("plan-problems")).toHaveTextContent("'waltz' is not a left-hand pattern");
    expect(api.sent("POST", "/jobs/arrange")).toHaveLength(0);
    // A good plan is sent with its label.
    await user.clear(editor);
    await user.paste(JSON.stringify({ sections: [{ start_bar: 1, end_bar: 2, lh_pattern: "pedal_tone", lh_voices: 1 }] }));
    await user.click(screen.getByTestId("plan-evaluate"));
    await waitFor(() => expect(api.sent("POST", "/jobs/arrange")).toHaveLength(1));
    expect(api.sent("POST", "/jobs/arrange")[0].body).toMatchObject({ label: "edited plan", plan: { sections: [{ lh_pattern: "pedal_tone", lh_voices: 1 }] } });
    expect(screen.getByTestId("tab-arrange")).toHaveAttribute("aria-selected", "true");
  });

  it("checks the arrangement against the current hands in the browser", async () => {
    api.on("GET", "/projects/p1", project({ current_arrangement_id: "a1" }, [ROW]));
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Arrangement 1" })).toBeInTheDocument());
    await user.click(screen.getByTestId("tab-advanced"));
    await user.click(screen.getByTestId("plan-check"));
    expect(screen.getByTestId("browser-verdict")).toHaveAttribute("data-status", "passes");
    expect(screen.getByTestId("browser-verdict")).toHaveTextContent("Fits your current hand profile");
    const score = scoreFromPlayback(PLAYBACK, "t");
    expect(score.notes).toHaveLength(4);
    expect(score.notes[0]).toMatchObject({ pitch: 72, onset: 0, bar: 1 });
    expect(handProfile({ max_span: 9, left_fingers: [1, 2] })).toMatchObject({ max_span: 9, comfortable_span: 10, leap_slack: 5 });
  });

  it("compares two revisions and labels one", async () => {
    const row2 = { ...ROW, id: "a2", revision: 2, accepted: true, n_hard: 0 };
    api.on("GET", "/projects/p1", project({ current_arrangement_id: "a2" }, [row2, ROW]));
    api.on("GET", "/projects/p1/arrangements/a2", { arrangement: { ...ARRANGEMENT, id: "a2", revision: 2 }, playback: PLAYBACK });
    api.on("GET", "/projects/p1/arrangements/a2/musicxml", textResponse(200, "<score-partwise/>", "application/xml"));
    api.on("GET", "/projects/p1/compare", (_c, url) => jsonResponse(200, {
      a: { revision: 1, accepted: false, hard: 1, strain: 0, fidelity: 0.92, difficulty: 3 }, b: { revision: 2, accepted: true, hard: 0, strain: 2, fidelity: 0.9, difficulty: 2 },
      profile_changes: [`max_span 12 → 14 (${url.searchParams.get("a")} vs ${url.searchParams.get("b")})`], plan_changes: ["Bars 1 to 2: block → pedal_tone"], same_source: true,
    }));
    api.on("PATCH", "/projects/p1/arrangements/a1", {});
    const user = userEvent.setup();
    renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Arrangement 2" })).toBeInTheDocument());
    await user.click(screen.getByTestId("tab-revisions"));
    await user.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 4, name: "What differs" })).toBeInTheDocument());
    expect(screen.getByText("Hands: max_span 12 → 14 (a1 vs a2)")).toBeInTheDocument();
    expect(screen.getByText(comparisonLine({ revision: 1, accepted: false, hard: 1, strain: 0, fidelity: 0.92, difficulty: 3 }))).toBeInTheDocument();
    const label = screen.getByLabelText("Label for arrangement 1");
    await user.type(label, "first try");
    await user.tab();
    await waitFor(() => expect(api.sent("PATCH", "/projects/p1/arrangements/a1")[0].body).toEqual({ label: "first try" }));
    await user.click(screen.getByRole("button", { name: "Open arrangement 1" }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Arrangement 1" })).toBeInTheDocument());
    expect(screen.getByTestId("tab-arrange")).toHaveAttribute("aria-selected", "true");
  });

  it("shows a failed job with a retry, and a load failure in words", async () => {
    api.on("GET", "/projects/p1", project());
    api.on("POST", "/jobs/arrange", jsonResponse(202, { job: { id: "j3" } }));
    api.on("GET", "/jobs/j3", { job: { id: "j3", status: "failed", progress: 0.4, stage: "render", result: null, error: { code: "render_failed", message: "The plan could not be rendered." } } });
    api.on("POST", "/jobs/j3/retry", jsonResponse(202, { job: { id: "j4" } }));
    api.on("GET", "/jobs/j4", { job: { id: "j4", status: "cancelled", progress: 0, stage: "", result: null, error: null } });
    const user = userEvent.setup();
    const first = renderView(<ProjectView projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("project-title")).toBeInTheDocument());
    await user.click(screen.getByTestId("tab-arrange"));
    await user.click(screen.getByTestId("arrange-button"));
    await waitFor(() => expect(within(screen.getByTestId("job-status")).getByRole("alert")).toHaveTextContent("The plan could not be rendered."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(api.sent("POST", "/jobs/j3/retry")).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("toast-info")).toHaveTextContent("Cancelled. Nothing was saved."));
    first.unmount();
    api.on("GET", "/projects/p9", apiError(404, "No such piece."));
    renderView(<ProjectView projectId="p9" />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Something went wrong"));
    expect(screen.getByText(/No such piece/)).toBeInTheDocument();
  });
});
