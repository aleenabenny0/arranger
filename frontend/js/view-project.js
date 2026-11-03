// One piece: inspect and correct the source, describe your hands, arrange,
// review the result, compare revisions, download.
//
// Each panel is built once and updated in place. Typing in a field never
// rebuilds the screen, so focus and the caret stay where they are. Server
// results are tied to the revision they were made from: an arrangement made
// from an older source or a different hand profile says so instead of
// passing as current.

import { api, newIdempotencyKey, watchJob } from "./api.js";
import { h, replace, field, toast, announce, tabs, dialog, formatDate, formatClock, pitchName } from "./dom.js";
import { Player } from "./player.js";
import { NotationView } from "./notation.js";

const KEY_NAMES = { "-7": "C♭", "-6": "G♭", "-5": "D♭", "-4": "A♭", "-3": "E♭", "-2": "B♭", "-1": "F", 0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F♯", 7: "C♯" };
const MINOR_NAMES = { "-7": "A♭", "-6": "E♭", "-5": "B♭", "-4": "F", "-3": "C", "-2": "G", "-1": "D", 0: "A", 1: "E", 2: "B", 3: "F♯", 4: "C♯", 5: "G♯", 6: "D♯", 7: "A♯" };
const RULE_NAMES = { range: "Out of range", hand_span: "Reach", hand_polyphony: "Too many notes in one hand", leap_infeasible: "Leap too fast",
  total_polyphony: "Too many notes at once", legato_break: "Note must be released early", hand_crossing: "Hands cross",
  fast_repetition: "Fast repeated note", finger_stretch: "Awkward between fingers" };
const FIDELITY_LABELS = [["melodic_recall", "Melody kept"], ["rhythm", "Rhythm kept"], ["contour", "Shape of the tune"],
  ["harmonic_coverage", "Harmony kept"], ["bass", "Bass line kept"], ["accompaniment", "Left hand kept busy"]];
const PROFILE_FIELDS = [
  ["max_span", "Largest reach (semitones)", "12 is an octave, 14 a ninth, 16 a tenth.", 1, 24],
  ["comfortable_span", "Comfortable reach (semitones)", "Wider than this is allowed but marked as a stretch.", 1, 24],
  ["max_notes_per_hand", "Notes one hand can hold at once", "", 1, 5],
  ["max_leap_rate", "Hand travel speed (semitones per second)", "70 suits most intermediate players.", 5, 400],
  ["skill_level", "Level, 1 to 10", "Decides which left-hand figures are offered.", 1, 10],
  ["lowest_pitch", "Lowest key (MIDI number)", "21 is the bottom A of a full piano.", 0, 127],
  ["highest_pitch", "Highest key (MIDI number)", "108 is the top C of a full piano.", 0, 127],
];

function keyName(key) {
  if (!key) return "unknown";
  const name = (key.mode === "minor" ? MINOR_NAMES : KEY_NAMES)[String(key.fifths)] || "?";
  return `${name} ${key.mode}${key.estimated ? " (estimated)" : ""}`;
}

function dl(pairs) {
  return h("dl", { class: "facts" }, pairs.filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([term, value]) => [h("dt", { text: term }), h("dd", { text: String(value) })]));
}

function findingsBadge(summary) {
  const icon = { passes: "✓", findings: "!", unresolved: "?" }[summary.status] || "i";
  return h("p", { class: `verdict verdict-${summary.status}` }, h("span", { class: "verdict-icon", "aria-hidden": "true", text: icon }),
    h("strong", { text: summary.headline }));
}

export async function projectView(root, state, projectId, setTitle) {
  const controller = new AbortController();
  const player = new Player();
  const catalog = state.catalog || { presets: [], capabilities: {}, calibration_steps: [] };
  let data = await api.get(`/projects/${projectId}`, { signal: controller.signal });
  let profile = { ...(data.project.profile || (catalog.presets.find((p) => p.id === "intermediate") || { profile: {} }).profile) };
  let profileAtLastArrange = data.project.profile ? JSON.stringify(data.project.profile) : null;
  let arrangement = null;
  let activeJob = null;
  setTitle(data.project.title);

  const heading = h("h1", { text: data.project.title });
  const subheading = h("p", { class: "muted" });
  const sourcePanel = h("div", {});
  const handsPanel = h("div", {});
  const resultPanel = h("div", {});
  const revisionsPanel = h("div", {});
  const advancedPanel = h("div", {});
  const tabset = tabs({ label: "Steps", items: [
    { id: "source", label: "1. Source", content: sourcePanel },
    { id: "hands", label: "2. Your hands", content: handsPanel },
    { id: "arrange", label: "3. Arrangement", content: resultPanel },
    { id: "revisions", label: "Revisions", content: revisionsPanel },
    { id: "advanced", label: "Advanced", content: advancedPanel },
  ], initial: data.project.current_arrangement_id ? "arrange" : "source", onChange: () => player.pause() });

  replace(root, h("p", {}, h("a", { href: "#/library", text: "← Your pieces" })), heading, subheading, tabset);

  async function reload() {
    data = await api.get(`/projects/${projectId}`, { signal: controller.signal });
    heading.textContent = data.project.title;
    subheading.textContent = [data.project.composer, data.source ? `source revision ${data.source.revision}` : ""].filter(Boolean).join(" · ");
    renderSource(); renderRevisions();
  }

  // ---------------------------------------------------------------- source --
  let sourceNotation = null;

  function renderSource() {
    const source = data.source;
    if (!source) { replace(sourcePanel, h("p", { text: "This piece has no source." })); return; }
    const info = source.inspection || {};
    const selection = source.selection || {};
    const isAudio = source.kind === "audio";

    const facts = dl([
      ["File", `${source.filename || "upload"} (${source.kind})`], ["Length", `${info.bar_count} bars, ${formatClock(info.duration_seconds || 0)}`],
      ["Notes", info.note_count], ["Key", keyName(info.key)], ["Meter", info.meter ? `${info.meter[0]}/${info.meter[1]}${info.meter_changes ? `, ${info.meter_changes} change(s)` : ""}` : "unknown"],
      ["Tempo", `${Math.round(info.tempo_bpm || 0)} beats per minute${info.tempo_changes ? `, ${info.tempo_changes} change(s)` : ""}`],
      ["Difficulty as written", info.difficulty ? `${info.difficulty.level} of 10 (${info.difficulty.label})` : ""],
    ]);

    const blocks = [h("h2", { text: "What was imported" }), facts];
    if (info.as_written) {
      blocks.push(h("h3", { text: "How the original sits under an average hand" }),
        h("ul", {}, h("li", { text: `Read literally, without pedal: ${info.as_written.without_pedal.headline}.` }),
          h("li", { text: `With the pedal changed each bar: ${info.as_written.with_pedal_each_bar.headline}.` })));
    }
    if (info.transcription) {
      blocks.push(h("div", { class: "notice", role: "note" }, h("strong", { text: "This came from a recording. " }), info.transcription.note,
        h("p", { text: `Model confidence ${Math.round((info.transcription.overall_confidence || 0) * 100)}%. ${info.confidence ? info.confidence.note : ""}` })));
    }
    if ((info.warnings || []).length) {
      blocks.push(h("h3", { text: "Things the import could not keep exactly" }), h("ul", { class: "warnings" }, info.warnings.map((w) => h("li", { text: w }))));
    }

    // --- corrections form. Values are read on submit; nothing re-renders while typing.
    const tracks = info.tracks || [];
    const melodyName = "melody-track";
    const auto = h("input", { type: "radio", name: melodyName, value: "", id: "melody-auto", checked: selection.melody_track === null || selection.melody_track === undefined });
    const trackRows = tracks.map((t) => {
      const radio = h("input", { type: "radio", name: melodyName, value: String(t.index), "aria-label": `Use ${t.name || `part ${t.index + 1}`} as the melody`, checked: selection.melody_track === t.index });
      const skip = h("input", { type: "checkbox", value: String(t.index), "aria-label": `Leave out ${t.name || `part ${t.index + 1}`}`, checked: (selection.ignored_tracks || []).includes(t.index) });
      skip.dataset.ignore = "1";
      return h("tr", {}, h("th", { scope: "row", text: t.name || `Part ${t.index + 1}` }), h("td", { text: String(t.note_count) }),
        h("td", { text: t.lowest_pitch !== null ? `${pitchName(t.lowest_pitch)} to ${pitchName(t.highest_pitch)}` : "" }),
        h("td", { text: t.index === info.suggested_melody_track ? "Likely the tune" : t.melody_likelihood !== null ? `${Math.round(t.melody_likelihood * 100)}%` : "" }),
        h("td", {}, radio), h("td", {}, skip));
    });
    const transpose = h("select", {}, Array.from({ length: 13 }, (_, i) => i - 6).map((n) =>
      h("option", { value: String(n), text: n === 0 ? "No change" : `${n > 0 ? "Up" : "Down"} ${Math.abs(n)} semitone${Math.abs(n) === 1 ? "" : "s"}`, selected: (selection.transpose || 0) === n })));
    const tempo = h("input", { type: "number", min: "20", max: "400", step: "1", value: selection.tempo_bpm || "" });
    const meterTop = h("input", { type: "number", min: "1", max: "32", value: selection.meter ? selection.meter[0] : "" });
    const meterBottom = h("select", {}, ["", "2", "4", "8", "16"].map((v) => h("option", { value: v, text: v || "Keep", selected: String(selection.meter ? selection.meter[1] : "") === v })));
    const removals = [];
    const lowIds = (info.confidence && info.confidence.low_confidence_note_ids) || [];

    const formError = h("p", { class: "form-error", role: "alert" }); formError.hidden = true;
    const save = h("button", { type: "submit", class: "button button-primary", text: "Save corrections" });
    const form = h("form", { class: "form", novalidate: true, onsubmit: async (event) => {
      event.preventDefault(); formError.hidden = true;
      const chosen = form.querySelector(`input[name="${melodyName}"]:checked`);
      const next = {
        melody_track: chosen && chosen.value !== "" ? Number(chosen.value) : null,
        ignored_tracks: Array.from(form.querySelectorAll("input[data-ignore]:checked"), (el) => Number(el.value)),
        transpose: Number(transpose.value),
        edits: [...(selection.edits || []), ...removals.filter((r) => r.box.checked).map((r) => ({ op: "delete", note_id: r.id }))],
      };
      if (tempo.value) next.tempo_bpm = Number(tempo.value);
      if (meterTop.value && meterBottom.value) next.meter = [Number(meterTop.value), Number(meterBottom.value)];
      save.disabled = true;
      try {
        await api.post(`/projects/${projectId}/sources`, { based_on: source.id, selection: next });
        toast("Corrections saved as a new revision of the source.", "success");
        await reload(); markStale();
      } catch (error) { formError.textContent = error.message; formError.hidden = false; announce(error.message, { urgent: true }); }
      finally { save.disabled = false; }
    } });

    if (tracks.length > 1) {
      form.append(h("fieldset", {}, h("legend", { text: "Which part is the tune?" }),
        h("p", { class: "hint", text: "Arranger guesses, but you can hear it. Choose the part with the melody, and leave out any part you do not want." }),
        h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Table, scroll sideways if it is cut off" }, h("table", {}, h("thead", {}, h("tr", {}, ["Part", "Notes", "Range", "Looks like a tune", "Melody", "Leave out"].map((t) => h("th", { scope: "col", text: t })))),
          h("tbody", {}, trackRows))),
        h("div", { class: "inline-choice" }, auto, h("label", { for: "melody-auto", text: "Let Arranger find the melody" }))));
    }
    form.append(field({ label: "Transpose the whole piece", control: transpose, hint: "For example into a key with fewer sharps or flats." }));
    if (isAudio || !info.meter) {
      form.append(h("fieldset", {}, h("legend", { text: "Tempo and meter you hear" }),
        h("p", { class: "hint", text: "A recording has no barlines. Set these so the bars land where you hear them." }),
        field({ label: "Beats per minute", control: tempo }), field({ label: "Beats in a bar", control: meterTop }), field({ label: "Beat unit", control: meterBottom })));
    }
    if (lowIds.length) {
      const items = lowIds.slice(0, 200).map((id) => {
        const box = h("input", { type: "checkbox", id: `rm-${id}` });
        removals.push({ id, box });
        return h("li", {}, box, h("label", { for: `rm-${id}`, text: ` Remove note ${id}` }));
      });
      form.append(h("fieldset", {}, h("legend", { text: `Notes the model was unsure about (${lowIds.length})` }),
        h("p", { class: "hint", text: "Listen in the Arrangement step's player, then tick any that are wrong." }), h("ul", { class: "checklist" }, items)));
    }
    form.append(formError, save);

    const notationHost = h("div", {});
    const showNotation = h("button", { type: "button", class: "button", text: "Show the source as notation", onclick: async () => {
      showNotation.disabled = true;
      try {
        const xml = await api.text(`/projects/${projectId}/sources/${source.id}/musicxml`, { signal: controller.signal });
        sourceNotation = new NotationView({ label: `Notation of the source, ${info.bar_count} bars` });
        replace(notationHost, sourceNotation.root);
        await sourceNotation.load(xml, []);
      } catch (error) { if (!error.aborted) toast(error.message, "error"); } finally { showNotation.disabled = false; }
    } });

    replace(sourcePanel, blocks, h("h2", { text: "Correct the import" }), form, h("h2", { text: "Look at it" }), showNotation, notationHost);
  }

  // ----------------------------------------------------------------- hands --
  const profileInputs = {};

  function renderHands() {
    const preset = h("select", {}, h("option", { value: "", text: "Custom" }),
      catalog.presets.map((p) => h("option", { value: p.id, text: p.id.replace(/_/g, " ") })));
    const presetHint = h("p", { class: "hint", "aria-live": "polite" });
    preset.addEventListener("change", () => {
      const chosen = catalog.presets.find((p) => p.id === preset.value);
      if (!chosen) return;
      profile = { ...chosen.profile };
      presetHint.textContent = chosen.description;
      syncInputs(); markStale();
    });
    const fields = PROFILE_FIELDS.map(([key, label, hint, min, max]) => {
      const input = h("input", { type: "number", min: String(min), max: String(max), step: key === "max_leap_rate" ? "5" : "1", value: profile[key] });
      const wrapper = field({ label, hint, control: input });
      input.addEventListener("change", () => {
        const value = Number(input.value);
        if (!Number.isFinite(value) || value < min || value > max) { wrapper.setError(`Enter a number from ${min} to ${max}.`); return; }
        wrapper.setError(""); profile[key] = value; preset.value = ""; markStale();
      });
      profileInputs[key] = input;
      return wrapper;
    });
    const fingerSets = [["left_fingers", "Left hand"], ["right_fingers", "Right hand"]].map(([key, label]) =>
      h("fieldset", {}, h("legend", { text: `${label}: fingers you can use` }),
        h("div", { class: "inline-choice" }, [1, 2, 3, 4, 5].map((finger) => {
          const id = `${key}-${finger}`;
          const box = h("input", { type: "checkbox", id, checked: (profile[key] || [1, 2, 3, 4, 5]).includes(finger) });
          box.addEventListener("change", () => {
            const chosen = [1, 2, 3, 4, 5].filter((f) => document.getElementById(`${key}-${f}`).checked);
            if (!chosen.length) { box.checked = true; announce("Keep at least one finger."); return; }
            profile[key] = chosen; preset.value = ""; markStale();
          });
          return [box, h("label", { for: id, text: finger === 1 ? "1 (thumb)" : String(finger) })];
        }))));
    function syncInputs() {
      for (const [key] of PROFILE_FIELDS) if (profileInputs[key]) profileInputs[key].value = profile[key];
      for (const key of ["left_fingers", "right_fingers"]) for (const f of [1, 2, 3, 4, 5]) {
        const box = document.getElementById(`${key}-${f}`); if (box) box.checked = (profile[key] || [1, 2, 3, 4, 5]).includes(f);
      }
    }
    replace(handsPanel, h("h2", { text: "Describe your hands" }),
      h("p", { text: "“Playable” depends on who is playing. These numbers are what Arranger checks every note against." }),
      field({ label: "Start from", control: preset }), presetHint,
      h("button", { type: "button", class: "button", text: "Measure my hands step by step", onclick: () => calibrate(syncInputs, preset) }),
      h("div", { class: "form grid-2" }, fields), fingerSets);
  }

  function calibrate(syncInputs, preset) {
    const steps = catalog.calibration_steps || [];
    const answers = { base_preset: preset.value || "intermediate", name: "My hands" };
    let index = 0;
    const prompt = h("p", {}); const hint = h("p", { class: "hint" });
    const input = h("input", { type: "number", step: "any", inputmode: "decimal" });
    const wrapper = field({ label: "Your answer", control: input });
    const counter = h("p", { class: "muted", "aria-live": "polite" });
    const nextButton = h("button", { type: "button", class: "button button-primary", text: "Next" });
    const skip = h("button", { type: "button", class: "button", text: "Skip this one" });
    const el = dialog({ title: "Measure your hands", body: h("div", { class: "form" }, counter, prompt, hint, wrapper),
      actions: [h("button", { type: "button", class: "button", text: "Cancel", onclick: () => el.close() }), skip, nextButton] });
    function show() {
      const step = steps[index];
      counter.textContent = `Question ${index + 1} of ${steps.length}`;
      prompt.textContent = step.ask; hint.textContent = step.hint || ""; input.value = ""; wrapper.setError(""); input.focus();
      nextButton.textContent = index === steps.length - 1 ? "Finish" : "Next";
    }
    async function advance(useAnswer) {
      if (useAnswer) {
        const value = Number(input.value);
        if (!input.value || !Number.isFinite(value) || value <= 0) { wrapper.setError("Enter a number, or skip this one."); return; }
        answers[steps[index].field] = value;
      }
      index += 1;
      if (index < steps.length) { show(); return; }
      try {
        const result = await api.post("/catalog/calibrate", answers);
        profile = result.profile; preset.value = ""; syncInputs(); markStale(); el.close();
        toast("Your hand profile was updated from your measurements.", "success");
      } catch (error) { index = steps.length - 1; wrapper.setError(error.message); }
    }
    nextButton.addEventListener("click", () => advance(true));
    skip.addEventListener("click", () => advance(false));
    if (steps.length) show(); else el.close();
  }

  // ------------------------------------------------------------ arrangement --
  const staleNotice = h("div", { class: "notice", role: "status" }); staleNotice.hidden = true;
  const jobBox = h("div", { class: "progress-box" }); jobBox.hidden = true;
  const resultBody = h("div", {});
  const useModel = h("input", { type: "checkbox", id: "use-model" });
  const generate = h("button", { type: "button", class: "button button-primary", text: "Arrange for my hands" });
  const modelAvailable = Boolean(catalog.capabilities.model_repair && catalog.capabilities.model_repair.available);

  function markStale() {
    if (!arrangement) { staleNotice.hidden = true; return; }
    const reasons = [];
    if (data.project.current_source_id !== arrangement.source_id) reasons.push("the source has been corrected since");
    if (profileAtLastArrange && JSON.stringify(profile) !== profileAtLastArrange) reasons.push("your hand profile has changed since");
    staleNotice.hidden = !reasons.length;
    if (reasons.length) replace(staleNotice, h("strong", { text: "This arrangement is out of date: " }), `${reasons.join(" and ")}. Arrange again to see the effect.`);
  }

  async function runJob(body, label) {
    if (activeJob) return;
    generate.disabled = true;
    const bar = h("progress", { max: "1", value: "0", "aria-label": label });
    const stage = h("p", { role: "status", text: "Starting…" });
    const cancel = h("button", { type: "button", class: "button", text: "Cancel" });
    replace(jobBox, h("h3", { text: label }), bar, stage, cancel); jobBox.hidden = false;
    try {
      const started = body.jobId ? { job: { id: body.jobId } }
        : await api.post(`/projects/${projectId}/arrangements`, body, { idempotencyKey: newIdempotencyKey(), signal: controller.signal });
      activeJob = started.job.id;
      cancel.addEventListener("click", async () => { cancel.disabled = true; stage.textContent = "Cancelling…"; try { await api.post(`/jobs/${activeJob}/cancel`); } catch (error) { toast(error.message, "error"); } });
      const job = await watchJob(activeJob, { signal: controller.signal, onUpdate: (j) => { bar.value = j.progress; stage.textContent = `${j.stage} (${Math.round(j.progress * 100)}%)`; } });
      if (job.status === "succeeded") {
        profileAtLastArrange = JSON.stringify(profile);
        await reload(); await openArrangement(job.result.arrangement_id);
        toast("Arrangement ready.", "success");
      } else if (job.status === "cancelled") {
        toast("Cancelled. Nothing was saved.", "info");
      } else {
        replace(jobBox, h("p", { class: "form-error", role: "alert", text: job.error ? job.error.message : "The arrangement failed." }),
          h("button", { type: "button", class: "button", text: "Try again", onclick: async () => {
            try { const retried = await api.post(`/jobs/${job.id}/retry`); activeJob = null; runJob({ jobId: retried.job.id }, label); } catch (error) { toast(error.message, "error"); } } }));
        return;
      }
      jobBox.hidden = true;
    } catch (error) {
      if (!error.aborted) { replace(jobBox, h("p", { class: "form-error", role: "alert", text: error.message })); }
    } finally { activeJob = null; generate.disabled = false; }
  }

  generate.addEventListener("click", () => runJob({ profile, use_model: useModel.checked }, "Arranging"));

  async function openArrangement(id) {
    const detail = await api.get(`/projects/${projectId}/arrangements/${id}`, { signal: controller.signal });
    arrangement = detail.arrangement;
    const summary = arrangement.summary;
    const verdict = arrangement.verdict;
    markStale();

    const notation = new NotationView({ label: `Notation of arrangement ${arrangement.revision}` });
    const findings = verdict.violations || [];
    const findingRows = findings.map((f) => h("tr", {},
      h("td", {}, h("span", { class: `pill pill-${f.severity}`, text: f.severity === "hard" ? "Beyond your limits" : "A stretch" })),
      h("td", { text: f.bar ? `Bar ${f.bar}` : formatClock(f.time) }), h("td", { text: f.hand === "L" ? "Left" : f.hand === "R" ? "Right" : "Both" }),
      h("td", { text: RULE_NAMES[f.rule] || f.rule }), h("td", { text: f.message + (f.certainty === "unproven" ? " (not proven: the search was cut short)" : "") }),
      h("td", {}, (f.note_ids || []).length ? h("button", { type: "button", class: "button button-small", text: "Show", "aria-label": `Show bar ${f.bar} in the score`, onclick: () => notation.reveal(f) }) : null)));

    // Player: the same events the server computed, source and result.
    player.stop(); player.tracks = {}; player.active = null;
    player.load("result", detail.playback);
    try {
      const src = await api.get(`/projects/${projectId}/sources/${arrangement.source_id}`, { signal: controller.signal });
      player.load("source", src.playback);
    } catch { /* the source revision may have been removed; the result still plays */ }
    player.use("result");
    const playButton = h("button", { type: "button", class: "button button-primary", text: "Play", onclick: () => player.toggle() });
    const seek = h("input", { type: "range", min: "0", max: String(player.duration), step: "0.1", value: "0", "aria-label": "Position" });
    const clock = h("span", { class: "clock", "aria-live": "off" });
    const speed = h("select", { "aria-label": "Speed" }, [0.5, 0.75, 1, 1.25].map((v) => h("option", { value: String(v), text: `${v}×`, selected: v === 1 })));
    const which = h("fieldset", { class: "inline-choice" }, h("legend", { class: "visually-hidden", text: "Listen to" }),
      ["result", "source"].map((name) => {
        const radio = h("input", { type: "radio", name: "listen", id: `listen-${name}`, value: name, checked: name === "result", disabled: !player.tracks[name] });
        radio.addEventListener("change", () => { player.use(name); seek.max = String(player.duration); });
        return [radio, h("label", { for: `listen-${name}`, text: name === "result" ? "Arrangement" : "Original" })];
      }));
    let dragging = false;
    seek.addEventListener("input", () => { dragging = true; });
    seek.addEventListener("change", () => { player.seek(Number(seek.value)); dragging = false; });
    speed.addEventListener("change", () => player.setSpeed(speed.value));
    const onChange = (event) => {
      const { time, playing, bar } = event.detail;
      playButton.textContent = playing ? "Pause" : "Play";
      if (!dragging) seek.value = String(time);
      clock.textContent = `${formatClock(time)} / ${formatClock(player.duration)} · bar ${bar}`;
      seek.setAttribute("aria-valuetext", `${formatClock(time)} of ${formatClock(player.duration)}, bar ${bar}`);
    };
    player.addEventListener("change", onChange);
    onChange({ detail: { time: 0, playing: false, bar: 1 } });

    // Downloads.
    const downloads = h("div", { class: "toolbar", role: "group", "aria-label": "Downloads" });
    const pdfAvailable = Boolean(catalog.capabilities.export_pdf && catalog.capabilities.export_pdf.available);
    for (const [kind, label] of [["midi", "MIDI"], ["musicxml", "MusicXML"], ["pdf", "Printable PDF"]]) {
      if (kind === "pdf" && !pdfAvailable) { downloads.append(h("span", { class: "muted", text: "PDF engraving is not installed on this server." })); continue; }
      const button = h("button", { type: "button", class: "button", text: `Download ${label}` });
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          let result = await api.post(`/projects/${projectId}/arrangements/${arrangement.id}/exports/${kind}`);
          if (result.job) {
            button.textContent = "Engraving…";
            const job = await watchJob(result.job.id, { signal: controller.signal, onUpdate: (j) => { button.textContent = `Engraving… ${Math.round(j.progress * 100)}%`; } });
            if (job.status !== "succeeded") throw new Error(job.error ? job.error.message : "Engraving was cancelled.");
            result = { artifact: { id: job.result.artifact_id } };
          }
          window.location.assign(api.downloadUrl(result.artifact.id));
        } catch (error) { if (!error.aborted) toast(error.message, "error"); }
        finally { button.disabled = false; button.textContent = `Download ${label}`; }
      });
      downloads.append(button);
    }

    const fidelity = summary.fidelity || {};
    replace(resultBody,
      h("h2", { text: `Arrangement ${arrangement.revision}${arrangement.label ? `: ${arrangement.label}` : ""}` }),
      h("p", { class: "muted", text: `Made ${formatDate(arrangement.created_at)} from source revision ${(data.sources.find((s) => s.id === arrangement.source_id) || {}).revision || "?"} · engine ${arrangement.algorithm_version}${arrangement.model ? ` · ${arrangement.model}` : ""}` }),
      h("div", { class: "cards" },
        h("article", { class: "card" }, h("h3", { text: "Can you play it?" }), findingsBadge(summary.findings), h("p", { text: summary.findings.detail }),
          h("p", { class: "muted", text: `${summary.findings.hard} beyond your limits · ${summary.findings.strain} stretches` })),
        h("article", { class: "card" }, h("h3", { text: "Is it still the piece?" }),
          h("p", {}, h("strong", { text: `Fidelity score ${Math.round((fidelity.score || 0) * 100)}%` })),
          h("p", { class: "muted", text: "How much of the tune, rhythm, harmony and bass can still be heard. It is not a count of notes: what was left out is listed below." }),
          h("ul", { class: "meters" }, FIDELITY_LABELS.map(([key, label]) => h("li", {}, h("span", { text: label }),
            h("meter", { min: "0", max: "1", low: "0.6", high: "0.85", optimum: "1", value: String(fidelity[key] ?? 0), "aria-label": label }),
            h("span", { text: `${Math.round((fidelity[key] ?? 0) * 100)}%` }))))),
        h("article", { class: "card" }, h("h3", { text: "How hard is it?" }),
          h("p", {}, h("strong", { text: `${summary.difficulty.level} of 10` }), ` (${summary.difficulty.label})`),
          h("p", { class: "muted", text: summary.difficulty.note }),
          summary.tempo_scale < 1 ? h("p", { text: `Suggested tempo: ${Math.round(summary.tempo_bpm)} beats per minute (${Math.round(summary.tempo_scale * 100)}% of the original).` }) : null)),
      h("h3", { text: "What was changed" }), h("ul", {}, (arrangement.report.sentences || []).map((s) => h("li", { text: s }))),
      h("h3", { text: "Listen" }), h("div", { class: "player", role: "group", "aria-label": "Playback" }, playButton, which, seek, clock, speed),
      h("h3", { text: "Download" }), downloads,
      h("h3", { text: "Score" }), notation.root,
      h("h3", { text: `Findings (${findings.length})` }),
      findings.length ? h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Table, scroll sideways if it is cut off" }, h("table", {}, h("caption", { class: "visually-hidden", text: "Playability findings, most serious first" }),
        h("thead", {}, h("tr", {}, ["Kind", "Where", "Hand", "What", "Detail", "Score"].map((t) => h("th", { scope: "col", text: t })))), h("tbody", {}, findingRows)))
        : h("p", { text: "Nothing in this arrangement is outside the limits you set." }),
      verdict.violations_truncated ? h("p", { class: "muted", text: `${verdict.violations_truncated} more findings are not listed.` }) : null);

    renderAdvanced();
    try {
      const xml = await api.text(`/projects/${projectId}/arrangements/${arrangement.id}/musicxml`, { signal: controller.signal });
      await notation.load(xml, findings);
    } catch (error) { if (!error.aborted) toast(error.message, "error"); }
  }

  function renderArrange() {
    replace(resultPanel, h("h2", { text: "Make an arrangement" }),
      h("p", { text: "Arranger keeps the tune, fits a left hand to your level, and checks every note against your hands." }),
      modelAvailable ? h("div", { class: "inline-choice" }, useModel, h("label", { for: "use-model", text: "Also let an AI model try to improve it. A text summary of the piece is sent to Anthropic; your file is not." })) : null,
      generate, jobBox, staleNotice, resultBody);
  }

  // -------------------------------------------------------------- revisions --
  function renderRevisions() {
    const items = data.arrangements || [];
    if (!items.length) { replace(revisionsPanel, h("h2", { text: "Revisions" }), h("p", { text: "No arrangements yet." })); return; }
    const options = () => items.map((a) => h("option", { value: a.id, text: `Arrangement ${a.revision}${a.label ? `: ${a.label}` : ""}` }));
    const a = h("select", {}, options()); const b = h("select", {}, options());
    if (items.length > 1) a.value = items[1].id;
    const compareOut = h("div", { role: "status" });
    const rows = items.map((item) => {
      const label = h("input", { type: "text", value: item.label || "", maxlength: 120, "aria-label": `Label for arrangement ${item.revision}` });
      label.addEventListener("change", async () => { try { await api.patch(`/projects/${projectId}/arrangements/${item.id}`, { label: label.value }); toast("Label saved.", "success"); } catch (error) { toast(error.message, "error"); } });
      return h("tr", {}, h("th", { scope: "row", text: String(item.revision) }), h("td", {}, label), h("td", { text: formatDate(item.created_at) }),
        h("td", { text: item.accepted ? "Passes" : `${item.n_hard} beyond limits` }), h("td", { text: `${Math.round(item.fidelity_score * 100)}%` }), h("td", { text: String(item.difficulty) }),
        h("td", {}, h("button", { type: "button", class: "button button-small", text: "Open", "aria-label": `Open arrangement ${item.revision}`, onclick: async () => { tabset.select("arrange"); await openArrangement(item.id); } })));
    });
    replace(revisionsPanel, h("h2", { text: "Revisions" }),
      h("p", { text: "Every arrangement is kept with the source, hand profile and engine version it was made from." }),
      h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Table, scroll sideways if it is cut off" }, h("table", {}, h("thead", {}, h("tr", {}, ["No.", "Label", "Made", "Playable", "Kept", "Difficulty", "Open"].map((t) => h("th", { scope: "col", text: t })))), h("tbody", {}, rows))),
      items.length > 1 ? h("div", { class: "form" }, h("h3", { text: "Compare two" }), field({ label: "First", control: a }), field({ label: "Second", control: b }),
        h("button", { type: "button", class: "button", text: "Compare", onclick: async () => {
          try {
            const diff = await api.get(`/projects/${projectId}/compare`, { query: { a: a.value, b: b.value } });
            const line = (r) => `Arrangement ${r.revision}: ${r.accepted ? "passes" : `${r.hard} beyond limits`}, ${r.strain} stretches, ${Math.round(r.fidelity * 100)}% kept, difficulty ${r.difficulty}.`;
            replace(compareOut, h("ul", {}, h("li", { text: line(diff.a) }), h("li", { text: line(diff.b) })),
              h("h4", { text: "What differs" }), h("ul", {}, [...diff.profile_changes.map((c) => `Hands: ${c}`), ...diff.plan_changes].map((t) => h("li", { text: t }))),
              !diff.profile_changes.length && !diff.plan_changes.length ? h("p", { text: "The plans and hand profiles are the same." }) : null,
              diff.same_source ? null : h("p", { text: "They were made from different revisions of the source." }));
          } catch (error) { toast(error.message, "error"); }
        } }), compareOut) : null);
  }

  // --------------------------------------------------------------- advanced --
  function renderAdvanced() {
    if (!arrangement) { replace(advancedPanel, h("h2", { text: "Advanced" }), h("p", { text: "Make an arrangement first. Its plan and the engine's diagnostics appear here." })); return; }
    const planText = h("textarea", { rows: "16", spellcheck: "false", class: "code" });
    planText.value = JSON.stringify(arrangement.plan, null, 2);
    const planField = field({ label: "Arrangement plan (JSON)", control: planText, hint: "The plan is a list of decisions, not notes. Edit it and evaluate to get a new revision." });
    const pre = (value) => h("pre", { class: "code", tabindex: "0", text: JSON.stringify(value, null, 2) });
    replace(advancedPanel, h("h2", { text: "Advanced" }), planField,
      h("button", { type: "button", class: "button", text: "Evaluate this plan", onclick: () => {
        let plan;
        try { plan = JSON.parse(planText.value); } catch { planField.setError("That is not valid JSON."); return; }
        planField.setError(""); tabset.select("arrange"); runJob({ profile, plan, label: "edited plan" }, "Evaluating your plan");
      } }),
      h("details", {}, h("summary", { text: "Engine report" }), pre(arrangement.report)),
      h("details", {}, h("summary", { text: "Verdict" }), pre(arrangement.verdict)),
      h("details", {}, h("summary", { text: "Hand profile used" }), pre(arrangement.profile)));
  }

  // ------------------------------------------------------------------ start --
  subheading.textContent = [data.project.composer, data.source ? `source revision ${data.source.revision}` : ""].filter(Boolean).join(" · ");
  renderSource(); renderHands(); renderArrange(); renderRevisions(); renderAdvanced();
  if (data.project.current_arrangement_id) await openArrangement(data.project.current_arrangement_id);
  const running = (data.jobs || []).find((j) => j.kind === "arrange");
  if (running) { tabset.select("arrange"); runJob({ jobId: running.id }, "Arranging"); }

  return () => { controller.abort(); player.dispose(); };
}
