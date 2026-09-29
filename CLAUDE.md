# Arranger

Takes a recording, produces sheet music that **this specific player** can
actually play, at a difficulty they can actually handle.

## The one rule that everything else depends on

**The model emits an `ArrangementPlan`. It never emits notation.**

A deterministic renderer turns plans into MusicXML. If you find yourself
writing code where an LLM produces MusicXML, note names, or MIDI events
directly, stop — that design has been tried and rejected, and the reasons are
in `docs/build-log/why-plans-not-notes.md`.

The split: the model makes *musical judgement* (what to keep, how to voice it,
what the left hand does). Code guarantees *correctness* (well-formed output,
notes on the page, spans within reach). Judgement is what models are good at.
Correctness is what they are unreliable at. Do not mix them.

## Architecture

A MIDI file, a MusicXML file or a recording goes in. MIDI, MusicXML and an
engraved PDF come out, with a verdict against one player's hands.

```
MIDI / MusicXML / audio ─→ adapters ─→ Score ─→ selection (user corrections)
                                                    ↓
                     planner ─→ ArrangementPlan ─→ render ─→ verify + fidelity
                        ↑                                        │
   engine: local repair, musical refinement  ←── repair ←────────┘
   agent:  optional model, bounded by attempts, seconds and cost
                                                    ↓
                     notation ─→ MusicXML, LilyPond ─→ PDF;  MIDI writer
```

Arranging is deterministic by default: `engine.arrange_deterministic` is
candidate zero and needs no network. The model is an optional improver on top
(`agent.arrange(model=...)`), and its output is only ever a plan.

| Module | Role | Actual deps today |
|---|---|---|
| `arranger.ir`, `arranger.timeline` | Note/Score data model; tempo, meter and key maps, pickup bars | stdlib only |
| `arranger.limits` | Import limits and the typed import errors | stdlib only |
| `arranger.profile` | The player's physical limits, presets, guided calibration | stdlib only |
| `arranger.verify` | **The oracle.** Playability constraints; `solver` is a phrase-based dynamic-programming hand assignment with FEASIBLE / INFEASIBLE / UNKNOWN | stdlib only |
| `arranger.io`, `arranger.adapters.midi_writer` | Hand-rolled MIDI reader and writer | stdlib only |
| `arranger.adapters.musicxml_reader`, `musicxml_writer` | MusicXML and compressed MusicXML in and out | stdlib only (expat) |
| `arranger.adapters.lilypond` | LilyPond source, and a sandboxed subprocess that engraves the PDF | stdlib; the LilyPond program at run time |
| `arranger.adapters.audio` | Recording to notes with the Basic Pitch ONNX model | `numpy`, `onnxruntime`, `soundfile`, `soxr` (optional `audio` extra) |
| `arranger.analysis` | Melody extraction, chord and bass detection | stdlib only |
| `arranger.notation` | Quantisation, voices, ties, tuplets, key-aware spelling; shared by both notation writers | stdlib only |
| `arranger.plan` | ArrangementPlan schema | stdlib only: dataclasses; pydantic was deliberately skipped, see the file's own docstring |
| `arranger.planner`, `arranger.musicianship` | Deterministic first draft: regions, candidates gated by skill and tempo | stdlib only |
| `arranger.render` | Plan → internal `Score` (left-hand realisation, voicing, tempo scaling) | stdlib only |
| `arranger.fidelity` | Melody, rhythm, contour, harmony, bass and accompaniment against the source | stdlib only |
| `arranger.repair`, `arranger.engine` | Violations → suggestions; deterministic local repair and refinement | stdlib only |
| `arranger.agent` | Bounded repair loop, calls the Claude API directly | `anthropic` (only when a model is passed) |
| `arranger.selection`, `arranger.explain` | User corrections to a source; the plain-language report and difficulty estimate | stdlib only |
| `arranger.application` | Use-case layer for CLI/API/worker entry points (`workflows.py`) | stdlib only |
| `arranger.adapters` | External file-format adapters | adapter-specific |
| `arranger.ports` | Protocols for infrastructure boundaries | stdlib only |
| `arranger_api` | FastAPI service: accounts, projects, revisions, jobs, artifacts; serves the built web app | `fastapi`, `argon2-cffi`, `psycopg`, `httpx`, `resend` |
| `frontend-react` | The web app: React, TypeScript, Vite; typed from the API's OpenAPI document; a browser-side preview verifier in `src/lib/verify.ts` that never overrules the server | `react`, `openapi-fetch`; build-time `vite`, `vitest`, `openapi-typescript` |
| `c/midi` | A bounded MIDI parser in C11 that mirrors `arranger.io` event for event; `scripts/midi_diff.py` diffs the two over the corpus and corrupted files | none; CMake, Unity (vendored) for tests |
| `firmware/esp32-player` | An ESP32 piezo player driven over UART by `scripts/send_to_esp32.py`; the frame protocol and note logic are plain C tested on the host | ESP-IDF 5.x to build; `pyserial` for the host script |

**Not built yet.** Nothing in `src/` implements these:
- **Source separation.** Audio transcription assumes one instrument. `demucs`
  is not used; a band recording is transcribed as if it were a piano.
- **Detailed fingering.** The solver assigns hands and checks that a chord fits
  the available fingers. It does not choose a finger for every note, and
  nothing prints fingering.
- **Beat tracking for audio.** A recording gets a tempo estimate and the user
  can correct tempo and meter; barlines are not inferred from the audio.
- **Lyrics, chord symbols, repeats and ornaments** in notation output.
- **CP-SAT.** The hand solver is dynamic programming in the standard library.
  OR-Tools was considered and not used, so `arranger.verify` keeps zero
  dependencies.
- **LangGraph orchestration.** `arranger.agent` is a plain Python loop. The
  `agent` extra in `pyproject.toml` still lists `langgraph`; nothing imports it.
- **Billing, teams, sharing.** Every project belongs to exactly one account.

**Built but not verified against the real thing:** the S3 artifact store (no
live bucket), the Dockerfile (no Docker on the development machine; CI builds
it), the Postgres project workflow (runs in CI only), audio accuracy (measured
on synthesised audio only), the model path (tested with fakes; no paid call
has been made), and the ESP32 firmware (compiled in CI with ESP-IDF, its
protocol tested on the host; never flashed to a board). `docs/build-log/limitations.md` has the detail.

**`arranger.verify` has zero third-party dependencies and must stay that way.**
It is the component every other component's correctness is measured against.
It cannot be allowed to break because a library changed under it.

See `docs/architecture.md` for the layer boundaries. New CLIs, APIs, web UI
handlers, and background workers should call `arranger.application` use cases
rather than wiring domain modules together themselves. New file formats belong
under `arranger.adapters`.

## Working rules

- Constraint changes require a test for the violating case **and** a test for
  a near-miss that must stay clean. False positives are the expensive failure
  mode here: they make the agent damage music that was already fine, and you
  will blame the model instead of the rule.
- New rules default to `STRAIN`, not `HARD`. Promote to `HARD` only after
  checking it against real human-made arrangements in `evals/corpus/`.
- Never widen a profile's limits to make a test pass. The profile describes a
  human being. If the arrangement doesn't fit, the arrangement is wrong.
- Every `Violation` must carry `measured`, `limit`, and enough of `pitches`
  /`bar`/`hand` for the repair agent to act without re-deriving anything.
- Run `python tests/test_constraints.py` before proposing any change to
  `verify/`. It runs in under a second; there is no excuse.

## Musical conventions

- Enharmonic spelling follows key context, always. Never emit a note name
  without knowing the key — that is the engraver's job, not the reducer's.
- Voice leading beats voicing prettiness. A smooth inner line is worth more
  than a fuller chord.
- The melody is sacred. `melodic_recall` below 0.9 is a failed arrangement no
  matter how good the harmony score looks.
- When reducing, drop doublings first, then inner voices, then bass movement,
  then harmonic colour. Never drop the melody, and never drop the root of a
  chord that establishes a key change.

## Corpus licensing

The eval corpus is public-domain and Creative Commons audio only (IMSLP,
Musopen, ccMixter). This keeps the benchmark redistributable, which is what
makes it a benchmark rather than a demo. Do not add commercial recordings to
`evals/corpus/`, even locally — it always leaks into a commit eventually.

## Current state

The product works end to end and is tested at every layer: upload (MIDI,
MusicXML, audio) → inspect and correct → describe your hands → arrange as a
background job → notation preview, playback and findings in the browser →
revise and compare → download MIDI, MusicXML and PDF → reopen later. The
deterministic engine arranges all 24 piece-and-profile pairs tried from the
public-domain corpus (`evals/corpus/`, see Corpus licensing above) with the
melody fully kept.

`docs/progress.md` is the running log and lists what is left.
`docs/backlog.md` has the acceptance criteria. Nothing in this codebase is
quietly further along than the **Not built yet** list above claims.
Known limitations live in `docs/build-log/limitations.md`: read it before
concluding that a bug is new.
