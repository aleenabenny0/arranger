const LH_PATTERNS = [
  "block",
  "pedal_tone",
  "broken_octave",
  "arpeggio",
  "alberti",
  "walking",
];

const SAMPLE_SCORE = {
  title: "over-ambitious chorale",
  tempo_bpm: 76,
  notes: [
    { pitch: 36, onset: 0.0, duration: 0.9, staff: 2, bar: 1 },
    { pitch: 55, onset: 0.0, duration: 0.9, staff: 2, bar: 1 },
    { pitch: 64, onset: 0.0, duration: 0.9, staff: 1, bar: 1 },
    { pitch: 67, onset: 0.0, duration: 0.9, staff: 1, bar: 1 },
    { pitch: 72, onset: 0.0, duration: 0.9, staff: 1, bar: 1 },
    { pitch: 31, onset: 1.0, duration: 0.9, staff: 2, bar: 2 },
    { pitch: 62, onset: 1.0, duration: 0.9, staff: 1, bar: 2 },
    { pitch: 60, onset: 1.95, duration: 0.05, staff: 2, bar: 3 },
    { pitch: 33, onset: 2.0, duration: 0.9, staff: 2, bar: 3 },
    { pitch: 110, onset: 2.0, duration: 0.9, staff: 1, bar: 3 },
  ],
};

const DEFAULT_PROFILE = {
  name: "me",
  instrument: "piano",
  lowest_pitch: 21,
  highest_pitch: 108,
  max_span: 12,
  comfortable_span: 9,
  max_notes_per_hand: 5,
  max_leap_rate: 90,
  leap_slack: 5,
  skill_level: 4,
};

let state = {
  score: clone(SAMPLE_SCORE),
  profile: clone(DEFAULT_PROFILE),
  plan: {
    title: "basic arrangement",
    target_skill: 4,
    sections: [
      {
        start_bar: 1,
        end_bar: 3,
        lh_pattern: "block",
        melody_shift: 0,
        lh_octave: 3,
        lh_voices: 3,
        roll_wide_chords: false,
        melody_fold_window: 0,
        label: "Opening",
      },
    ],
    reductions: [],
    pedal_bars: [],
    notes: "Basic editable plan.",
  },
  verdict: null,
  arranged: null,
  fidelity: null,
  verdictSource: "local",
  backendStatus: "Local preview mode",
  saved: null,
  savedRecords: { scores: [], plans: [], arrangements: [] },
  savedView: "scores",
  storageStatus: "Nothing saved yet",
  user: null,
  authStatus: "Sign in to save work.",
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

const els = {
  scoreSummary: document.querySelector("#scoreSummary"),
  loadSampleBtn: document.querySelector("#loadSampleBtn"),
  apiUrl: document.querySelector("#apiUrl"),
  saveWorkBtn: document.querySelector("#saveWorkBtn"),
  runBackendBtn: document.querySelector("#runBackendBtn"),
  authEmail: document.querySelector("#authEmail"),
  authPassword: document.querySelector("#authPassword"),
  loginBtn: document.querySelector("#loginBtn"),
  registerBtn: document.querySelector("#registerBtn"),
  resetPasswordBtn: document.querySelector("#resetPasswordBtn"),
  refreshSavedBtn: document.querySelector("#refreshSavedBtn"),
  savedList: document.querySelector("#savedList"),
  logoutBtn: document.querySelector("#logoutBtn"),
  signedOutPanel: document.querySelector("#signedOutPanel"),
  signedInPanel: document.querySelector("#signedInPanel"),
  currentUser: document.querySelector("#currentUser"),
  authStatus: document.querySelector("#authStatus"),
  exportPlanBtn: document.querySelector("#exportPlanBtn"),
  exportVerdictBtn: document.querySelector("#exportVerdictBtn"),
  scoreFile: document.querySelector("#scoreFile"),
  profileFile: document.querySelector("#profileFile"),
  sourceTitle: document.querySelector("#sourceTitle"),
  sourceBars: document.querySelector("#sourceBars"),
  sourceNotes: document.querySelector("#sourceNotes"),
  sourceTempo: document.querySelector("#sourceTempo"),
  profileName: document.querySelector("#profileName"),
  skillLevel: document.querySelector("#skillLevel"),
  maxSpan: document.querySelector("#maxSpan"),
  comfortableSpan: document.querySelector("#comfortableSpan"),
  maxNotesPerHand: document.querySelector("#maxNotesPerHand"),
  maxLeapRate: document.querySelector("#maxLeapRate"),
  sectionsList: document.querySelector("#sectionsList"),
  addSectionBtn: document.querySelector("#addSectionBtn"),
  scoreViz: document.querySelector("#scoreViz"),
  timeline: document.querySelector("#timeline"),
  scoreJson: document.querySelector("#scoreJson"),
  planJson: document.querySelector("#planJson"),
  verdictJson: document.querySelector("#verdictJson"),
  verdictPanel: document.querySelector("#verdictPanel"),
  verdictEyebrow: document.querySelector("#verdictEyebrow"),
  verdictTitle: document.querySelector("#verdictTitle"),
  backendStatus: document.querySelector("#backendStatus"),
  storageStatus: document.querySelector("#storageStatus"),
  hardCount: document.querySelector("#hardCount"),
  strainCount: document.querySelector("#strainCount"),
  violationsList: document.querySelector("#violationsList"),
  planHealth: document.querySelector("#planHealth"),
};

function pitchName(midi) {
  const names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  return `${names[((midi % 12) + 12) % 12]}${Math.floor(midi / 12) - 1}`;
}

function offset(note) {
  return Number(note.onset) + Number(note.duration);
}

function soundsAt(note, time) {
  const eps = 0.000001;
  return Number(note.onset) - eps <= time && time < offset(note) - eps;
}

function onsets(score) {
  return [...new Set(score.notes.map((n) => Number(n.onset)))].sort((a, b) => a - b);
}

function soundingAt(score, time) {
  return score.notes.filter((note) => soundsAt(note, time));
}

function centroid(pitches) {
  if (!pitches.length) return null;
  return pitches.reduce((sum, pitch) => sum + pitch, 0) / pitches.length;
}

function assignHands(sounding, profile, previous = [null, null]) {
  if (!sounding.length) return [{}, previous];

  if (sounding.some((note) => note.staff !== undefined && note.staff !== null)) {
    const assignment = {};
    sounding.forEach((note, index) => {
      assignment[index] = (note.staff || 1) >= 2 ? "L" : "R";
    });
    return [assignment, centroidsFor(sounding, assignment, previous)];
  }

  const order = sounding
    .map((note, index) => [note.pitch, index])
    .sort((a, b) => a[0] - b[0])
    .map((item) => item[1]);
  let best = null;

  for (let split = 0; split <= order.length; split += 1) {
    const leftIndexes = order.slice(0, split);
    const rightIndexes = order.slice(split);
    const leftPitches = leftIndexes.map((index) => sounding[index].pitch);
    const rightPitches = rightIndexes.map((index) => sounding[index].pitch);
    let cost = 0;

    [leftPitches, rightPitches].forEach((pitches) => {
      if (!pitches.length) return;
      const span = Math.max(...pitches) - Math.min(...pitches);
      if (span > profile.max_span) cost += 1000 * (span - profile.max_span);
      else if (span > profile.comfortable_span) {
        cost += 5 * (span - profile.comfortable_span);
      }
      if (pitches.length > profile.max_notes_per_hand) {
        cost += 1000 * (pitches.length - profile.max_notes_per_hand);
      }
    });

    const leftCentroid = centroid(leftPitches);
    const rightCentroid = centroid(rightPitches);
    [
      [leftCentroid, previous[0]],
      [rightCentroid, previous[1]],
    ].forEach(([current, prev]) => {
      if (current === null) return;
      cost += prev === null ? 24 : Math.abs(current - prev);
    });
    if (leftCentroid !== null && rightCentroid !== null && leftCentroid > rightCentroid) {
      cost += 500;
    }

    const assignment = {};
    leftIndexes.forEach((index) => {
      assignment[index] = "L";
    });
    rightIndexes.forEach((index) => {
      assignment[index] = "R";
    });

    if (!best || cost < best.cost) best = { cost, assignment };
  }

  return [best.assignment, centroidsFor(sounding, best.assignment, previous)];
}

function centroidsFor(sounding, assignment, previous) {
  const left = [];
  const right = [];
  sounding.forEach((note, index) => {
    if (assignment[index] === "L") left.push(note.pitch);
    if (assignment[index] === "R") right.push(note.pitch);
  });
  return [centroid(left) ?? previous[0], centroid(right) ?? previous[1]];
}

function makeViolation(fields) {
  return {
    bar: null,
    hand: null,
    pitches: [],
    measured: 0,
    limit: 0,
    message: "",
    ...fields,
  };
}

function verifyScore(score, profile) {
  const violations = [];

  score.notes.forEach((note) => {
    if (note.pitch < profile.lowest_pitch || note.pitch > profile.highest_pitch) {
      violations.push(
        makeViolation({
          rule: "range",
          severity: "hard",
          time: note.onset,
          bar: note.bar ?? null,
          pitches: [note.pitch],
          measured: note.pitch,
          limit: note.pitch < profile.lowest_pitch ? profile.lowest_pitch : profile.highest_pitch,
          message: `${pitchName(note.pitch)} is outside ${pitchName(profile.lowest_pitch)}-${pitchName(profile.highest_pitch)}`,
        }),
      );
    }
  });

  let previousCentroids = [null, null];
  const lastPlayed = [null, null];

  onsets(score).forEach((time) => {
    const sounding = soundingAt(score, time);
    if (!sounding.length) return;
    const [assignment, centroids] = assignHands(sounding, profile, previousCentroids);
    const bar = sounding.find((note) => note.bar !== undefined && note.bar !== null)?.bar ?? null;
    const active = new Set(Object.values(assignment));

    ["L", "R"].forEach((hand) => {
      const pitches = sounding
        .filter((_, index) => assignment[index] === hand)
        .map((note) => note.pitch)
        .sort((a, b) => a - b);
      if (!pitches.length) return;

      const span = pitches[pitches.length - 1] - pitches[0];
      if (span > profile.max_span) {
        violations.push(
          makeViolation({
            rule: "hand_span",
            severity: "hard",
            time,
            bar,
            hand,
            pitches,
            measured: span,
            limit: profile.max_span,
            message: `${hand}H must span ${span} semitones (${pitchName(pitches[0])}-${pitchName(pitches[pitches.length - 1])}).`,
          }),
        );
      } else if (span > profile.comfortable_span) {
        violations.push(
          makeViolation({
            rule: "hand_span",
            severity: "strain",
            time,
            bar,
            hand,
            pitches,
            measured: span,
            limit: profile.comfortable_span,
            message: `${hand}H stretch of ${span} semitones is reachable but tiring.`,
          }),
        );
      }

      if (pitches.length > profile.max_notes_per_hand) {
        violations.push(
          makeViolation({
            rule: "hand_polyphony",
            severity: "hard",
            time,
            bar,
            hand,
            pitches,
            measured: pitches.length,
            limit: profile.max_notes_per_hand,
            message: `${hand}H needs ${pitches.length} fingers.`,
          }),
        );
      }
    });

    ["L", "R"].forEach((hand, index) => {
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
        violations.push(
          makeViolation({
            rule: "leap_infeasible",
            severity: "hard",
            time,
            bar,
            hand,
            measured: displacement,
            limit: Number(budget.toFixed(2)),
            message: `${hand}H moves ${displacement.toFixed(0)} semitones in ${(dt * 1000).toFixed(0)}ms.`,
          }),
        );
      }
    });

    previousCentroids = centroids;
  });

  const totalLimit = profile.max_notes_per_hand * 2;
  onsets(score).forEach((time) => {
    const sounding = soundingAt(score, time);
    if (sounding.length > totalLimit) {
      violations.push(
        makeViolation({
          rule: "total_polyphony",
          severity: "hard",
          time,
          bar: sounding.find((note) => note.bar !== undefined && note.bar !== null)?.bar ?? null,
          pitches: sounding.map((note) => note.pitch).sort((a, b) => a - b),
          measured: sounding.length,
          limit: totalLimit,
          message: `${sounding.length} notes sounding at once; only ${totalLimit} fingers.`,
        }),
      );
    }
  });

  violations.sort((a, b) => a.time - b.time || a.rule.localeCompare(b.rule));
  const hard = violations.filter((violation) => violation.severity === "hard");
  return {
    title: score.title || "untitled",
    profile: profile.name || "custom",
    playable: hard.length === 0,
    n_hard: hard.length,
    n_strain: violations.filter((violation) => violation.severity === "strain").length,
    violations,
  };
}

function validatePlan(plan, score) {
  const problems = [];
  if (!plan.sections?.length) problems.push("Plan has no sections.");
  const covered = new Map();
  const lastBar = getLastBar(score);

  plan.sections?.forEach((section, index) => {
    if (section.start_bar > section.end_bar) problems.push(`Section ${index + 1}: start bar is after end bar.`);
    if (section.start_bar < 1) problems.push(`Section ${index + 1}: bars start at 1.`);
    if (section.lh_voices < 0 || section.lh_voices > 5) problems.push(`Section ${index + 1}: LH voices must be 0-5.`);
    if (section.melody_fold_window && (section.melody_fold_window < 7 || section.melody_fold_window > 24)) {
      problems.push(`Section ${index + 1}: fold window must be 0 or 7-24.`);
    }
    for (let bar = section.start_bar; bar <= section.end_bar; bar += 1) {
      if (covered.has(bar)) problems.push(`Bar ${bar} is covered by more than one section.`);
      covered.set(bar, index);
    }
  });

  for (let bar = 1; bar <= lastBar; bar += 1) {
    if (!covered.has(bar)) problems.push(`Bar ${bar} is not covered by any section.`);
  }

  return problems;
}

function getLastBar(score) {
  return Math.max(1, ...score.notes.map((note) => note.bar || 1));
}

function syncProfileFromInputs() {
  state.profile.name = els.profileName.value || "custom";
  state.profile.skill_level = Number(els.skillLevel.value || 1);
  state.profile.max_span = Number(els.maxSpan.value || 1);
  state.profile.comfortable_span = Number(els.comfortableSpan.value || 1);
  state.profile.max_notes_per_hand = Number(els.maxNotesPerHand.value || 1);
  state.profile.max_leap_rate = Number(els.maxLeapRate.value || 1);
}

function renderProfileInputs() {
  els.profileName.value = state.profile.name || "";
  els.skillLevel.value = state.profile.skill_level ?? 5;
  els.maxSpan.value = state.profile.max_span ?? 12;
  els.comfortableSpan.value = state.profile.comfortable_span ?? 9;
  els.maxNotesPerHand.value = state.profile.max_notes_per_hand ?? 5;
  els.maxLeapRate.value = state.profile.max_leap_rate ?? 70;
}

function renderSourceStats() {
  const bars = getLastBar(state.score);
  els.sourceTitle.textContent = state.score.title || "untitled";
  els.sourceBars.textContent = bars;
  els.sourceNotes.textContent = state.score.notes.length;
  els.sourceTempo.textContent = `${state.score.tempo_bpm || 100} bpm`;
  els.scoreSummary.textContent = `${state.score.title || "untitled"} - ${bars} bars, ${state.score.notes.length} notes`;
}

function renderSections() {
  els.sectionsList.innerHTML = "";
  state.plan.sections.forEach((section, index) => {
    const row = document.createElement("div");
    row.className = "section-row";
    row.innerHTML = `
      <label>Start<input data-field="start_bar" type="number" min="1" step="1" value="${section.start_bar}"></label>
      <label>End<input data-field="end_bar" type="number" min="1" step="1" value="${section.end_bar}"></label>
      <label>Pattern<select data-field="lh_pattern">${LH_PATTERNS.map((pattern) => `<option value="${pattern}">${pattern}</option>`).join("")}</select></label>
      <label>Voices<input data-field="lh_voices" type="number" min="0" max="5" step="1" value="${section.lh_voices}"></label>
      <label>Octave<input data-field="lh_octave" type="number" min="0" max="6" step="1" value="${section.lh_octave}"></label>
      <label>Shift<input data-field="melody_shift" type="number" min="-24" max="24" step="1" value="${section.melody_shift}"></label>
      <label>Fold<input data-field="melody_fold_window" type="number" min="0" max="24" step="1" value="${section.melody_fold_window}"></label>
      <button class="remove-section" type="button" aria-label="Remove section">x</button>
    `;
    row.querySelector("select").value = section.lh_pattern;
    row.querySelectorAll("input, select").forEach((control) => {
      control.addEventListener("input", () => {
        const field = control.dataset.field;
        section[field] = control.tagName === "SELECT" ? control.value : Number(control.value);
        markUnsaved();
        refresh();
      });
    });
    row.querySelector(".remove-section").addEventListener("click", () => {
      state.plan.sections.splice(index, 1);
      markUnsaved();
      refresh();
    });
    els.sectionsList.appendChild(row);
  });
}

function renderVerdict() {
  const verdict = state.verdict;
  els.verdictEyebrow.textContent = state.verdictSource === "backend" ? "Arranged Verdict" : "Source Verdict";
  els.backendStatus.textContent = state.backendStatus;
  els.verdictPanel.classList.toggle("good", verdict.playable);
  els.verdictPanel.classList.toggle("bad", !verdict.playable);
  els.verdictTitle.textContent = verdict.playable ? "Playable" : "Not Playable";
  els.hardCount.textContent = verdict.n_hard;
  els.strainCount.textContent = verdict.n_strain;

  els.violationsList.innerHTML = "";
  if (!verdict.violations.length) {
    els.violationsList.innerHTML = `<div class="health-item"><strong>No issues found</strong><p>This score fits the current profile.</p></div>`;
  } else {
    verdict.violations.slice(0, 20).forEach((violation) => {
      const item = document.createElement("div");
      item.className = `violation ${violation.severity}`;
      const where = violation.bar ? `Bar ${violation.bar}` : `${violation.time.toFixed(2)}s`;
      item.innerHTML = `<strong>${where} - ${violation.rule}</strong><p>${violation.message}</p>`;
      els.violationsList.appendChild(item);
    });
  }
}

function renderStorageStatus() {
  els.storageStatus.textContent = state.storageStatus;
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function renderSavedList() {
  els.savedList.innerHTML = "";
  if (!state.user) {
    els.savedList.innerHTML = `<div class="empty-state">Sign in to load saved work.</div>`;
    return;
  }

  const records = state.savedRecords[state.savedView] || [];
  if (!records.length) {
    els.savedList.innerHTML = `<div class="empty-state">No saved ${state.savedView} yet.</div>`;
    return;
  }

  records.slice(0, 12).forEach((record) => {
    const item = document.createElement("button");
    item.className = "saved-item";
    item.type = "button";
    const title = record.title || record.name || record.payload?.title || record.payload?.name || record.id;
    const meta = state.savedView === "arrangements"
      ? `${record.status} - ${record.n_hard} hard - ${formatDate(record.created_at)}`
      : formatDate(record.updated_at || record.created_at);
    item.innerHTML = `<strong>${title}</strong><span>${meta}</span>`;
    item.addEventListener("click", () => loadSavedRecord(state.savedView, record));
    els.savedList.appendChild(item);
  });
}

function renderAuth() {
  els.signedOutPanel.classList.toggle("hidden", Boolean(state.user));
  els.signedInPanel.classList.toggle("hidden", !state.user);
  els.currentUser.textContent = state.user
    ? `${state.user.display_name} (${state.user.email})`
    : "Signed in";
  els.authStatus.textContent = state.authStatus;
}

function renderPlanHealth() {
  const problems = validatePlan(state.plan, state.score);
  els.planHealth.innerHTML = "";
  if (!problems.length) {
    const message = state.verdictSource === "backend"
      ? `Rendered by backend. Fidelity score: ${state.fidelity?.score?.toFixed(2) ?? "-"}`
      : "Sections are valid. Run the backend to verify the arranged score.";
    els.planHealth.innerHTML = `<div class="health-item"><strong>Plan covers the score</strong><p>${message}</p></div>`;
    return;
  }
  problems.forEach((problem) => {
    const item = document.createElement("div");
    item.className = "health-item";
    item.innerHTML = `<strong>Needs attention</strong><p>${problem}</p>`;
    els.planHealth.appendChild(item);
  });
}

function renderScoreViz() {
  const score = state.arranged || state.score;
  const notes = score.notes || [];
  els.scoreViz.innerHTML = "";
  if (!notes.length) {
    els.scoreViz.innerHTML = `<div class="empty-state">No notes to show.</div>`;
    return;
  }

  const minPitch = Math.min(...notes.map((note) => note.pitch));
  const maxPitch = Math.max(...notes.map((note) => note.pitch));
  const end = Math.max(...notes.map((note) => offset(note)));
  const pitchRange = Math.max(1, maxPitch - minPitch);
  const safeEnd = Math.max(1, end);

  notes.slice(0, 400).forEach((note) => {
    const marker = document.createElement("div");
    marker.className = `note-marker staff-${note.staff || 0}`;
    marker.title = `${pitchName(note.pitch)} at ${note.onset}s`;
    marker.style.left = `${(Number(note.onset) / safeEnd) * 100}%`;
    marker.style.width = `${Math.max(1.2, (Number(note.duration) / safeEnd) * 100)}%`;
    marker.style.bottom = `${((note.pitch - minPitch) / pitchRange) * 88 + 4}%`;
    els.scoreViz.appendChild(marker);
  });
}

function renderTimeline() {
  const byBar = new Map();
  state.verdict.violations.forEach((violation) => {
    if (!violation.bar) return;
    const current = byBar.get(violation.bar) || { hard: 0, strain: 0 };
    current[violation.severity] += 1;
    byBar.set(violation.bar, current);
  });

  els.timeline.innerHTML = "";
  for (let bar = 1; bar <= getLastBar(state.score); bar += 1) {
    const counts = byBar.get(bar) || { hard: 0, strain: 0 };
    const tile = document.createElement("div");
    tile.className = `bar-tile ${counts.hard ? "hard" : counts.strain ? "strain" : ""}`;
    tile.innerHTML = `<strong>Bar ${bar}</strong><small>${counts.hard} hard, ${counts.strain} strain</small>`;
    els.timeline.appendChild(tile);
  }
}

function renderJson() {
  els.scoreJson.value = JSON.stringify(state.score, null, 2);
  els.planJson.value = JSON.stringify(state.plan, null, 2);
  els.verdictJson.value = JSON.stringify(
    {
      verdict_source: state.verdictSource,
      saved: state.saved,
      verdict: state.verdict,
      fidelity: state.fidelity,
      arranged: state.arranged,
    },
    null,
    2,
  );
}

function refresh() {
  syncProfileFromInputs();
  if (state.verdictSource !== "backend") {
    state.verdict = verifyScore(state.score, state.profile);
    state.arranged = null;
    state.fidelity = null;
    state.backendStatus = "Local preview mode";
  }
  renderSourceStats();
  renderSections();
  renderVerdict();
  renderAuth();
  renderStorageStatus();
  renderPlanHealth();
  renderScoreViz();
  renderTimeline();
  renderJson();
}

function downloadJson(name, value) {
  const blob = new Blob([JSON.stringify(value, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

function cookieValue(name) {
  const prefix = `${name}=`;
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
    ?.slice(prefix.length) || "";
}

function jsonHeaders(includeCsrf = false) {
  const headers = { "Content-Type": "application/json" };
  const csrfToken = cookieValue("arranger_csrf");
  if (includeCsrf && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  return headers;
}

async function loadJsonFile(file) {
  return JSON.parse(await file.text());
}

async function runBackend() {
  syncProfileFromInputs();
  state.backendStatus = "Contacting backend...";
  renderVerdict();

  const baseUrl = apiBaseUrl();
  try {
    const response = state.saved
      ? await fetch(`${baseUrl}/arrangements/render-and-verify`, {
          method: "POST",
          credentials: "include",
          headers: jsonHeaders(true),
          body: JSON.stringify({
            score_id: state.saved.score_id,
            profile_id: state.saved.profile_id,
            plan_id: state.saved.plan_id,
          }),
        })
      : await fetch(`${baseUrl}/render-and-verify`, {
          method: "POST",
          headers: jsonHeaders(false),
          body: JSON.stringify({
            source: state.score,
            profile: state.profile,
            plan: state.plan,
          }),
        });

    const body = await response.json();
    if (!response.ok) {
      const detail = body.detail?.detail || body.detail || "Backend request failed.";
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }

    if (body.record) {
      state.arranged = body.record.arranged_score;
      state.verdict = body.record.verdict;
      state.fidelity = body.record.fidelity;
      state.storageStatus = `Saved arrangement ${body.record.id}`;
    } else {
      state.arranged = body.arranged;
      state.verdict = body.verdict;
      state.fidelity = body.fidelity;
    }
    state.verdictSource = "backend";
    state.backendStatus = "Verified with Python backend";
    renderSourceStats();
    renderSections();
    renderVerdict();
    renderStorageStatus();
    renderPlanHealth();
    renderTimeline();
    renderJson();
  } catch (error) {
    state.verdictSource = "local";
    state.verdict = verifyScore(state.score, state.profile);
    state.arranged = null;
    state.fidelity = null;
    state.backendStatus = `Backend unavailable: ${error.message}`;
    renderVerdict();
    renderStorageStatus();
    renderPlanHealth();
    renderTimeline();
    renderJson();
  }
}

async function postJson(path, payload) {
  const baseUrl = apiBaseUrl();
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    credentials: "include",
    headers: jsonHeaders(true),
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail?.detail || body.detail || "Backend request failed.";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

async function getJson(path) {
  const baseUrl = apiBaseUrl();
  const response = await fetch(`${baseUrl}${path}`, {
    method: "GET",
    credentials: "include",
    headers: jsonHeaders(false),
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail?.detail || body.detail || "Backend request failed.";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

async function putJson(path, payload) {
  const baseUrl = apiBaseUrl();
  const response = await fetch(`${baseUrl}${path}`, {
    method: "PUT",
    credentials: "include",
    headers: jsonHeaders(true),
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail?.detail || body.detail || "Backend request failed.";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

async function authRequest(path, payload = null) {
  const baseUrl = apiBaseUrl();
  const options = {
    method: payload ? "POST" : "GET",
    credentials: "include",
    headers: jsonHeaders(path === "/auth/logout"),
  };
  if (payload) options.body = JSON.stringify(payload);

  const response = await fetch(`${baseUrl}${path}`, options);
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail?.detail || body.detail || "Auth request failed.";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function apiBaseUrl() {
  return els.apiUrl.value.replace(/\/$/, "");
}

async function loginOrRegister(path) {
  try {
    const email = els.authEmail.value.trim();
    const password = els.authPassword.value;
    const payload = path === "/auth/register"
      ? { email, password, display_name: email.split("@", 1)[0] }
      : { email, password };
    const body = await authRequest(path, payload);
    state.user = body.user;
    state.authStatus = "Signed in";
    els.authPassword.value = "";
    renderAuth();
    await loadSavedRecords();
  } catch (error) {
    state.authStatus = error.message;
    renderAuth();
  }
}

async function bootstrapAuth() {
  try {
    const body = await authRequest("/auth/me");
    state.user = body.user;
    state.authStatus = "Signed in";
    renderAuth();
    await loadSavedRecords();
  } catch {
    state.user = null;
    renderAuth();
    renderSavedList();
  }
}

async function logout() {
  try {
    await authRequest("/auth/logout", {});
  } finally {
    state.user = null;
    state.saved = null;
    state.savedRecords = { scores: [], plans: [], arrangements: [] };
    state.storageStatus = "Nothing saved yet";
    state.authStatus = "Signed out";
    renderAuth();
    renderStorageStatus();
    renderSavedList();
  }
}

async function resetPassword() {
  try {
    const email = els.authEmail.value.trim();
    if (!email) throw new Error("Enter your email first.");
    const request = await authRequest("/auth/password-reset/request", { email });
    if (!request.reset_token) {
      state.authStatus = "Reset requested. Email delivery is the next provider step.";
      renderAuth();
      return;
    }
    const password = window.prompt("New password");
    if (!password) return;
    await authRequest("/auth/password-reset/confirm", {
      token: request.reset_token,
      password,
    });
    state.authStatus = "Password reset. You can log in now.";
    renderAuth();
  } catch (error) {
    state.authStatus = error.message;
    renderAuth();
  }
}

async function saveWork() {
  syncProfileFromInputs();
  state.storageStatus = "Saving workspace...";
  renderStorageStatus();

  try {
    const profile = state.saved?.profile_id
      ? await putJson(`/profiles/${state.saved.profile_id}`, state.profile)
      : await postJson("/profiles", state.profile);
    const score = state.saved?.score_id
      ? { record: await getJson(`/scores/${state.saved.score_id}`).then((body) => body.record) }
      : await postJson("/scores", state.score);
    const plan = state.saved?.plan_id
      ? await putJson(`/plans/${state.saved.plan_id}`, state.plan)
      : await postJson("/plans", {
          score_id: score.record.id,
          plan: state.plan,
        });
    state.saved = {
      profile_id: profile.record.id,
      score_id: score.record.id,
      plan_id: plan.record.id,
    };
    state.storageStatus = `Saved score ${state.saved.score_id}`;
    await loadSavedRecords();
    renderStorageStatus();
    renderJson();
  } catch (error) {
    state.saved = null;
    state.storageStatus = `Save failed: ${error.message}`;
    renderStorageStatus();
  }
}

async function loadSavedRecords() {
  if (!state.user) return;
  try {
    const [scores, plans, arrangements] = await Promise.all([
      getJson("/scores"),
      getJson("/plans"),
      getJson("/arrangements"),
    ]);
    state.savedRecords = {
      scores: scores.records,
      plans: plans.records,
      arrangements: arrangements.records,
    };
    renderSavedList();
  } catch (error) {
    state.storageStatus = `Load saved work failed: ${error.message}`;
    renderStorageStatus();
  }
}

async function loadSavedRecord(kind, record) {
  try {
    if (kind === "scores") {
      state.score = clone(record.payload);
      state.saved = { score_id: record.id, profile_id: null, plan_id: null };
      state.storageStatus = `Loaded score ${record.id}`;
    }
    if (kind === "plans") {
      const score = await getJson(`/scores/${record.score_id}`);
      state.score = clone(score.record.payload);
      state.plan = clone(record.payload);
      state.saved = { score_id: record.score_id, profile_id: null, plan_id: record.id };
      state.storageStatus = `Loaded plan ${record.id}`;
    }
    if (kind === "arrangements") {
      const [score, plan, profile] = await Promise.all([
        getJson(`/scores/${record.score_id}`),
        getJson(`/plans/${record.plan_id}`),
        getJson(`/profiles/${record.profile_id}`),
      ]);
      state.score = clone(score.record.payload);
      state.plan = clone(plan.record.payload);
      state.profile = clone(profile.record.payload);
      state.arranged = clone(record.arranged_score);
      state.verdict = clone(record.verdict);
      state.fidelity = clone(record.fidelity);
      state.verdictSource = "backend";
      state.backendStatus = "Loaded previous arrangement";
      state.saved = {
        score_id: record.score_id,
        profile_id: record.profile_id,
        plan_id: record.plan_id,
      };
      state.storageStatus = `Loaded arrangement ${record.id}`;
      renderProfileInputs();
      refresh();
      return;
    }
    state.verdictSource = "local";
    renderProfileInputs();
    refresh();
  } catch (error) {
    state.storageStatus = `Load failed: ${error.message}`;
    renderStorageStatus();
  }
}

function markUnsaved() {
  state.saved = null;
  state.storageStatus = "Unsaved changes";
  state.verdictSource = "local";
}

els.loadSampleBtn.addEventListener("click", () => {
  state.score = clone(SAMPLE_SCORE);
  state.profile = clone(DEFAULT_PROFILE);
  state.arranged = null;
  state.fidelity = null;
  state.verdictSource = "local";
  state.saved = null;
  state.storageStatus = "Nothing saved yet";
  state.plan.sections = [
    {
      start_bar: 1,
      end_bar: getLastBar(state.score),
      lh_pattern: "block",
      melody_shift: 0,
      lh_octave: 3,
      lh_voices: 3,
      roll_wide_chords: false,
      melody_fold_window: 0,
      label: "Opening",
    },
  ];
  renderProfileInputs();
  refresh();
});

els.exportPlanBtn.addEventListener("click", () => downloadJson("arrangement-plan.json", state.plan));
els.exportVerdictBtn.addEventListener("click", () => downloadJson("verdict.json", {
  verdict_source: state.verdictSource,
  saved: state.saved,
  verdict: state.verdict,
  fidelity: state.fidelity,
  arranged: state.arranged,
}));
els.runBackendBtn.addEventListener("click", runBackend);
els.saveWorkBtn.addEventListener("click", saveWork);
els.refreshSavedBtn.addEventListener("click", loadSavedRecords);
els.loginBtn.addEventListener("click", () => loginOrRegister("/auth/login"));
els.registerBtn.addEventListener("click", () => loginOrRegister("/auth/register"));
els.resetPasswordBtn.addEventListener("click", resetPassword);
els.logoutBtn.addEventListener("click", logout);

els.scoreFile.addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  state.score = await loadJsonFile(file);
  markUnsaved();
  if (!state.plan.sections.length) {
    state.plan.sections.push({
      start_bar: 1,
      end_bar: getLastBar(state.score),
      lh_pattern: "block",
      melody_shift: 0,
      lh_octave: 3,
      lh_voices: 3,
      roll_wide_chords: false,
      melody_fold_window: 0,
      label: "Imported",
    });
  }
  refresh();
});

els.profileFile.addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  state.profile = { ...DEFAULT_PROFILE, ...(await loadJsonFile(file)) };
  markUnsaved();
  renderProfileInputs();
  refresh();
});

[els.profileName, els.skillLevel, els.maxSpan, els.comfortableSpan, els.maxNotesPerHand, els.maxLeapRate].forEach((control) => {
  control.addEventListener("input", () => {
    markUnsaved();
    refresh();
  });
});

els.addSectionBtn.addEventListener("click", () => {
  const last = state.plan.sections[state.plan.sections.length - 1];
  const start = last ? last.end_bar + 1 : 1;
  state.plan.sections.push({
    start_bar: start,
    end_bar: Math.max(start, getLastBar(state.score)),
    lh_pattern: "block",
    melody_shift: 0,
    lh_octave: 3,
    lh_voices: 2,
    roll_wide_chords: false,
    melody_fold_window: 0,
    label: "",
  });
  markUnsaved();
  refresh();
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`#${tab.dataset.view}View`).classList.add("active");
  });
});

document.querySelectorAll(".saved-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".saved-tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    state.savedView = tab.dataset.saved;
    renderSavedList();
  });
});

els.scoreJson.addEventListener("change", () => {
  state.score = JSON.parse(els.scoreJson.value);
  markUnsaved();
  refresh();
});

els.planJson.addEventListener("change", () => {
  state.plan = JSON.parse(els.planJson.value);
  markUnsaved();
  refresh();
});

els.apiUrl.value = window.location.protocol.startsWith("http")
  ? window.location.origin
  : "http://127.0.0.1:8000";
renderProfileInputs();
refresh();
bootstrapAuth();
