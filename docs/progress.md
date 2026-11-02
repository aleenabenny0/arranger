# Progress log

Newest entries first. Records what was done, why, what was tested, and what is
left. Backlog item ids refer to `docs/backlog.md`.

## 2026-11-02 - Item 10: the engine beats brute force, and the arranger tests itself

**Test state.** `python -m pytest --ignore=tests/test_browser_e2e.py`:
942 passed, 22 skipped (Postgres and LocalStack, run in CI); with `--cov=src`,
91% line coverage against the 90 gate. `ruff check .`: clean. `python
tests/test_constraints.py`: 19/19. New test files: `test_engine_quality.py`
(12), `test_evaluate.py` (11), `test_mutopia.py` (12), `test_autotest.py`
(21); `test_agent.py` gained two and changed one (below), `test_notation.py`
gained one. Nothing in the suite touches the network: the catalogue tests
run against recorded pages in `tests/fixtures/mutopia/`.

### Why

The pre-registered evaluation (`docs/build-log/eval-protocol.md`) compares
the model with brute force on cost alone, and brute force reaches cost 0 on
nearly every piece, so the question "does the arranger beat exhaustive
search" could not be asked on most music, and nothing stopped the
deterministic engine from losing to it quietly. The owner asked for the
software and the model to win, and for the arranger to find music online
and test itself.

### Done

| Item | What exists | Evidence |
|---|---|---|
| F1 | `engine.quality_key`: one ordering for every comparison. Cost (hard findings plus the fidelity shortfall), then tempo, then strain, then fidelity above the floor to two decimal places (`planner.FIDELITY_RESOLUTION`; closer is a tie, so a per-region plan is not flattened by a uniform one that places one more note). `Candidate.better_than` uses it; so do the planner's draft choice, local repair, the model loop's best-so-far and the harness | `tests/test_engine_quality.py` |
| F1 | The engine tries every skill-legal whole-piece figure itself (`engine.seed_uniform`, easiest first, bounded) before the musical refinements, and a new `polish` pass feeds STRAIN findings through the repair rules for a plan that already plays, never trading tempo, cost or the floor for comfort. The engine therefore cannot lose to brute force on the ordering; the test asserts it on every fixture and preset | same |
| F1 | Planner: a strain-aware per-region key (`STRAIN_PENALTY` charges a figure's musical prior per strain finding) with fidelity as the last tie-break | `tests/test_planner.py` unchanged |
| F1 | Repair: `suggest_repairs(..., include_strain=True)` offers the same edits for STRAIN findings; the default is unchanged | `test_strain_findings_only_produce_suggestions_when_asked` |
| F1 | Model loop: the prompt now lists every pattern (stride and broken tenth were missing), every plan lever (`roll_wide_chords`, `voicing`, `bass`, `harmonic_rhythm`, `pedal_bars`, `tempo_scale`) and the ranking order; the first message and every piece of feedback say exactly what a better plan must do (`_what_would_win`); a plan that is accepted but no better than the draft no longer ends the run as a success; `RunResult.best_strain` is recorded | `tests/test_agent.py` |
| F1 | `agent.brute_force_candidate`: the fully scored exhaustive search, skill-legal by default with the protocol's unfiltered search available, ties among equally cheap uniform plans broken on the same ordering so a contestant cannot "win" by picking a better tie-break among plans brute force already tried. `brute_force_baseline` keeps its tuple shape | `test_brute_force_candidate_is_the_best_uniform_plan_on_quality` |
| F1 | `arranger.evaluate` rewritten: brute force, the deterministic engine and (with `--model`) the model loop on every piece; wins "on cost" (the protocol, strict) and "on quality"; the protocol's PASS / FAIL / INSUFFICIENT DATA verdict computed only from qualifying pieces with model runs; `--json`; the corpus as the default input | `tests/test_evaluate.py`, `SCORECARD.md` |
| F2 | `adapters.mutopia`: search table, piece page and RSS feed parsed with `html.parser` and `ElementTree`; a client that identifies itself, waits a second between requests, caps page and file sizes, retries only on 429 and 5xx, never leaves the site, and refuses to download anything whose licence is not Public Domain or permissive Creative Commons | `tests/test_mutopia.py` against recorded pages |
| F2 | `application.autotest` and the `arranger-autotest` CLI: pieces from the corpus, files or discovery; 16-bar snippets cut exactly on the timeline; invariants on real music (MIDI, score JSON and MusicXML round trips, plan validity, verifier determinism, melody kept, engine reproducibility on snippets); the contest per profile; a committed baseline (`evals/autotest/baseline.json`) that fails the run on a regression; a Markdown report that credits licence and source page; a refused file is a result, a crash is a failure | `tests/test_autotest.py` |
| F2 | CI job `autotest`: the 15 short corpus pieces, three profiles, whole and in snippets, against the baseline, report in the job summary. `.github/workflows/autotest.yml`: weekly and on demand, the whole corpus plus one page of Mutopia's piano listing (the run number picks the page), with inputs for page, limit, instrument and an optional model | both YAML files parse; neither has run on GitHub yet |
| - | Docs: README, CLAUDE.md table, `docs/architecture.md`, `docs/algorithm.md`, `docs/backlog.md` (section F), `docs/build-log/limitations.md`, `docs/build-log/eval-protocol.md` amendment, `evals/corpus/README.md`, changelog | - |

### Measured

`python -m arranger.evaluate` on the 23 corpus pieces, profile `me`, engine
only (no network): **on cost 2 wins, 21 ties, 0 losses; on quality 9 wins,
14 ties, 0 losses.** The two cost wins are the concerto movements: Beethoven
op. 37 from 1.00 to 0.00 (at 40% tempo, which the ordering charges before
strain), Tchaikovsky op. 35 from 4.00 to 2.00. The quality wins at equal
cost are fidelity gains of 0.01 to 0.04 at equal strain. Engine time is 1.5
to 4 times brute force (see limitations).

`arranger-autotest --discover mutopia --page 3 --limit 3`, live: three Bach
inventions found (two Public Domain, one CC BY-SA 3.0), 6 contests, 6 wins
on quality, 0 failures, all 6 or 7 invariants holding on each; the downloads
and their provenance landed in the git-ignored `evals/discovered/`.

`arranger-autotest --corpus` with profiles `me`, `beginner` and `advanced`,
whole pieces and three 16-bar snippets each, is what `evals/autotest/baseline.json`
records: 71 cases, 213 contests, **106 wins, 107 ties, 0 losses on quality**.
The beginner profile is where judgement shows: brute force leaves cost 4 to
555 on 25 pieces and snippets, the engine clears all but two of them to 0.
That list is the report's "Resists brute force" section, the pool a model
would have to prove itself on.

### Found by the self-tester on its first corpus run, and fixed

- **The MusicXML export of the Für Elise arrangement came back two notes
  short.** Two of the famous repeated E's are 32nd-note flicks before the
  beat; the quantiser moved them onto the next E's position, where the
  notation step de-duplicates a pitch struck twice in one chord. The
  merge is a real limit of the grid (there are no grace notes) but it was
  silent, against the rule that every lossy step warns. `notation.notate`
  now counts the merges and says so, with a test for the merge and two
  near-misses (a different pitch at the same spot becomes a chord; the same
  pitch against a longer neighbour becomes a second voice). The round-trip
  invariant accepts a loss only up to the declared count; an undeclared
  loss, or a note from nowhere, still fails.

### Found by the first CI run, and fixed

- **The baseline comparison flagged unchanged results as regressions.** It
  rounded the recorded fidelity to three places and the fresh one to four,
  so a piece at 0.9226 read as having fallen from 0.923. No local run had
  exercised the comparison on real data because the local runs were all
  `--update-baseline`, which skips it. Both sides now round to the quality
  ordering's resolution, and a test feeds the same number through with
  several decimal tails.
- **Bandit refused two things in the catalogue adapter:** the RSS feed was
  parsed with `xml.etree`, which expands entities on untrusted input, and
  `urlopen` took a URL whose scheme was checked one level up. The feed is
  now read with regular expressions (four flat fields; a hostile entity
  stays text), and the fetch function refuses anything that is not https on
  the site's own host before the request is built.
- **pip-audit on the lock file:** `certifi` 2026.7.22 carries a revoked
  root (PYSEC-2026-4320), published after the last green run on 2026-09-29.
  Pinned to 2026.10.19 in `requirements.lock`; nothing in this branch
  touched the lock otherwise.

### Changed expectations in existing tests (call out at handoff)

- `test_agent.py::test_loop_stops_as_soon_as_it_succeeds` now stubs the
  engine with an unacceptable draft, because on its fixture the engine's own
  draft is always accepted and a single-section model plan can never beat it.
  The old assertion was guarded by `if result.playable` and passed vacuously.
- `test_engine_quality.py::test_planner_keeps_its_per_region_judgement_over_a_uniform_near_tie`
  records the decision that a fidelity difference under a hundredth is a tie.

### Decided

- Fidelity resolution is two decimal places. At three, a uniform fallback won
  the planner's mixed-texture fixture by 0.005 and flattened the plan; the
  renderer's note placement is not a musical preference at that scale.
- Brute force is held to the same skill ceiling as every other plan; the
  unfiltered number is still computed and listed where it differs, so the
  protocol's historical figures stay comparable.
- The model cannot end a run by merely matching the draft. "Accepted" was a
  property of the plan; "better than the free candidate" is the property that
  justifies paying for it.

### Not done

- No paid model run has been made. The scorecard's verdict line reads
  INSUFFICIENT DATA and will until `python -m arranger.evaluate --model
  claude-sonnet-5` is run with a key.
- The `autotest` CI job and the weekly workflow have not run on GitHub.
- Only Mutopia is read. IMSLP and Musopen have no adapter; both would need
  their own licence parsing.
- The quality wins at equal cost and strain are fidelity gains, and fidelity
  saturates (limitations); a listening test is the honest next check.
- The engine is slower on very long pieces; bounded by the job deadline.

## 2026-09-29 - Item 09: a C MIDI parser and an ESP32 player

**Test state.** `ctest --test-dir c/midi/build`: 3 tests (31 Unity cases)
passed, built with `-Wall -Wextra -Wpedantic -Wshadow -Wconversion -Werror`
under MinGW-w64 GCC 16.2; CI repeats it under ASan and UBSan.
`scripts/midi_diff.py --generated 100 --mutations 300 evals/corpus/*.mid`:
423 files, 285 parsed identically, 138 refused identically, 0 differ.
`ctest --test-dir firmware/esp32-player/host/build`: 2 tests (15 Unity cases)
passed. `python -m pytest tests/test_esp32_sender.py tests/test_midi_raw.py`:
11 passed. The full Python suite still passes (see the CI run for the branch).

### Done

| Item | What exists | Evidence |
|---|---|---|
| 09 | `c/midi/`: a bounded Standard MIDI File parser in C11 (formats 0, 1 and 2, variable-length quantities, running status, tempo and signature meta events, notes closed oldest first, held notes closed at the end of the track, every read bounds-checked, limits from `ImportLimits`), a JSON dumper, and Unity tests including every prefix and every single-byte corruption of a file | `c/midi/tests/test_midi.c`, the `c-midi` job |
| 09 | `arranger.io.read_midi_raw` exposes the Python reader's raw view through the same chunk walk `read_midi_bytes` uses; `scripts/midi_diff.py` parses files with both and diffs them, and generates pieces and corrupted variants from a fixed seed | `tests/test_midi_raw.py`, the diff run above |
| 09 | `firmware/esp32-player/`: an ESP-IDF application (UART receive task, LEDC PWM on a piezo, activity LED, ACK for every frame, release of held notes when the host goes quiet) over two plain-C components: the framed CRC-8 protocol with a resyncing parser, and the held-note set for a one-voice sounder with an equal-temperament frequency table | `firmware/esp32-player/host/` under CTest; the `firmware-host` and `esp32-build` jobs |
| 09 | `scripts/send_to_esp32.py`: reads MIDI, MusicXML or score JSON with the project's readers and streams timed frames over pyserial (`--melody-only`, `--speed`, `--tone` wiring check, `--dry-run`); its encoder and parser are the C protocol's twins, pinned by the same golden bytes | `tests/test_esp32_sender.py` |
| 09 | CI: `c-midi` (CMake, CTest, sanitizers, the diff over the corpus with 300 generated and 1000 corrupted files), `firmware-host` (host tests with sanitizers, the host script's tests), `esp32-build` (`idf.py build` in Espressif's ESP-IDF 5.2 image, the `.bin` files kept as an artifact) | `.github/workflows/ci.yml` |

### Not done

- Nothing has been flashed: there is no board and no ESP-IDF on the
  development machine. The build in CI proves the firmware compiles and links;
  `firmware/esp32-player/README.md` has the wiring and the flashing steps.
- The player has one voice and no device-side clock; the host keeps time.
- Sanitized builds run only in CI: MinGW-w64 on Windows has no ASan.

## 2026-09-29 - Item 08: the web app rewritten in React and TypeScript

**Test state.** `python -m pytest --ignore=tests/test_browser_e2e.py`: 872
passed, 22 skipped. `tests/test_browser_e2e.py` against the built app: 3
passed. `e2e/` (Playwright): 15 passed. `frontend-react`: `tsc --noEmit`
clean; Vitest 81 passed, 91.04% of lines covered. Both browser suites passed
on the React build unchanged, which was the acceptance test for the rewrite.

### Done

| Item | What exists | Evidence |
|---|---|---|
| 08 | `frontend-react/`: Vite 6, React 19, TypeScript 5.9. `openapi.json` exported from the app and turned into `src/api/schema.ts` by `openapi-typescript`; `src/api/client.ts` is `openapi-fetch` with a middleware for the CSRF header and an `ApiError` per failure, so a route or field that does not exist is a compile error | `npm run typecheck`, `src/api/client.test.ts` |
| 08 | Views: home, sign in and register, forgot and reset password, email verification, library (upload with progress, search, paging, rename and delete dialogs), account, and the piece (source facts and corrections, hands with presets and step-by-step measurement, arranging through `POST /jobs/arrange` with progress, cancel and retry, the result with verdict, fidelity, difficulty, playback, downloads, notation with findings marked, the findings table, revisions and comparison, the advanced plan editor with the engine's JSON) | `src/views/*.test.tsx`, both browser suites |
| 08 | Components: WAI-ARIA `Tabs`, native `<dialog>` `Dialog` and `ConfirmDialog` with focus returned to the opener, `Field`, `Notation` (Verovio adopted node by node, styles through a constructed stylesheet), `PlayerControls`, and a new `Timeline` of bar tiles that seeks the player and reveals the bar's finding | `src/components/components.test.tsx` |
| 08 | The browser fallback verifier ported to TypeScript (`src/lib/verify.ts`) with `validatePlan`; the Advanced view refuses a malformed plan before sending it and can check the current arrangement against the current hands in the browser, labelled as a browser check | `src/lib/verify.test.ts`, `project.test.tsx` |
| 08 | Served from `frontend-react/dist` (`FRONTEND_DIR` default); `/assets/` is fingerprinted and cached immutable, everything else revalidated. Multi-stage `Dockerfile` (Node build stage, then the Python image copies `dist/`); `fetch_vendor.py --target`; CI job `frontend` (type check, Vitest with coverage and a JUnit file, build, forbidden-pattern greps) and a build step in every job that serves the app | `tests/test_api.py::test_frontend_is_served_from_root`, `tests/test_api_security.py::test_static_frontend_assets_stay_cacheable`, `.github/workflows/ci.yml` |
| 08 | The old static app under `frontend/` removed once both browser suites passed on the build | git history |

### Found by the unit tests

- **Signing out could land on the sign-in page instead of home.** The route
  guard read the route state, which only catches up on the `hashchange`
  event, so a sign-out from the library navigated home and was then
  redirected to `/login`. The Playwright test did not notice because its
  assertions (a sign-in link and the toast) hold on either page. The guard now
  reads the live address.

### Not done

- Firefox, Safari and real assistive technology are still untested.
- The app is built for the root of an origin (`/assets/...`); hosting under a
  sub-path needs Vite's `base` set.

## 2026-09-19 - Checkpoint 4: frontend, browser verification, planner, operations

**Test state.** `pytest`: 831 passed, 1 skipped (the Postgres journey; needs
`TEST_DATABASE_URL`, runs in CI). `ruff check .`: clean. The three browser tests
are included in that count and take about 20 seconds.

### Done since checkpoint 3

| Item | What exists | Evidence |
|---|---|---|
| E1 | Static frontend rebuilt on the project routes: `frontend/index.html`, `styles.css`, `js/{app,api,dom,player,notation,view-library,view-project,view-account,legal}.js`. Hash routing, no framework, no build step, no `innerHTML`. The old JSON plan editor (`frontend/app.js`) is removed; it was unmodified, so it is recoverable from git | `tests/test_browser_e2e.py`, `tests/test_api.py` |
| E1 | Browser journey in Edge/Chromium against a live server with a real worker: register, verify by emailed link, upload, read the inspection, show the source as notation, correct the melody part (revision 2), choose a preset, arrange, read verdict and report, Verovio notation with note ids, play and pause, compare with the original, download and parse MIDI, MusicXML and PDF, arrange again for other hands, compare revisions, reload and reopen, export the account, read all five legal pages | same |
| E1 | axe-core (WCAG 2.0/2.1/2.2 A and AA plus best practice) on every view; a second run with the keyboard alone at 320 pixels wide in the dark theme with reduced motion; a third for a bad upload and a wrong password. Each fails on any console error, page error, failed request or unexpected 4xx/5xx | same |
| E3 | `privacy.html`, `terms.html`, `cookies.html`, `copyright.html`, `support.html`: written from what the code does, operator details filled from `/legal/config`, "not provided" and a Draft banner until they are set | browser test; `docs/legal-review.md` |
| B3 | Planner: the skill ceiling from `patterns_for_skill` is a hard filter on candidates; stride (needs a hand that can cover 24 semitones in a beat) and rolled broken tenths (at or under 84 bpm) offered from skill 7; a `stop` signal polled before every trial, and the engine gives the planner half of the job's time | `tests/test_planner.py` (16) |
| - | `notation.key_name`: an estimated minor key is named by its own tonic ("A minor"), not "C major's relative minor" | `tests/test_notation.py` |
| D3 | `tests/test_artifacts.py`: one contract test over the local, database and S3 stores; SigV4 checked against the worked example in the AWS documentation; S3 against a fake bucket; hostile keys, bucket names and download filenames | 35 tests |
| - | `/ready` now also checks the file store (cached for 30 seconds) and answers 503 `artifact_store_unavailable` | `tests/test_api_projects.py` |
| E4 | `Dockerfile`: LilyPond, libsndfile, the audio packages, `requirements.lock` as constraints, `fetch_vendor.py` at build, non-root user, `/data` volume. **Not built locally: no Docker on this machine** | CI job `docker-build` builds it, starts it, and fails unless `/catalog` reports PDF and audio as available |
| E4 | `.github/workflows/ci.yml`: whole suite on 3.12 and 3.13 with LilyPond and the audio model; browser job with screenshots kept; forbidden-pattern grep on the frontend; `pip-audit` on the lock file; the project journey against real Postgres; image smoke test | YAML parses; the grep guard and every test command were run locally |
| E4 | `requirements.lock` from a clean environment (also proved `pip install ".[api]"` imports and starts); extras `model` and `e2e`; `fetch_vendor.py --dev` for axe-core | clean-venv install |
| E4 | `docs/deployment.md` rewritten; new `docs/runbook.md` (backups, restore, rollback, alerts, incidents), `docs/security.md` (controls, the test behind each, 12 known limitations), `docs/legal-review.md` | every command, metric name, label, endpoint and setting named in them was checked against the code |
| - | `CLAUDE.md` architecture table and "Not built yet", `README.md`, `docs/architecture.md`, `docs/artifact-storage.md`, `docs/build-log/limitations.md`, `frontend/README.md`, `src/arranger_api/README.md` brought up to date | - |

### Defects found by the browser and clean-install checks, and fixed

- **`argon2-cffi` was imported but not declared.** `pip install ".[api]"` in a
  clean environment, and therefore the Docker image, would have crashed at
  import. Added to the `api` extra.
- **The skip link navigated away.** `href="#main"` collided with hash routing and
  sent keyboard users to the home view. It now moves focus to the content.
- **First load stole focus from the skip link.** Focus moves to the new heading
  on navigation only, not on the first render.
- **Concurrent first start on SQLite crashed.** Switching a new file to WAL
  needs an exclusive lock that SQLite does not wait for, so a web process and a
  worker starting together could fail with "database is locked". The switch is
  retried within the busy timeout. Found because a longer suite changed timing
  and exposed a flaky test (3 failures in 5 runs before, 0 in 16 after).
- **Application modules were cached for an hour without fingerprints**, so a
  deploy could leave a browser running a new module against an old one. App
  code is now revalidated; only the versioned notation engine is cached.
- Toasts were outside any landmark; two tables had empty header cells; no
  favicon (a 404 on every page load).
- "100% of the original kept" overclaimed when notes had been left out. It now
  reads "Fidelity score", with a sentence saying it is not a count of notes.
- The S3 store's docstring claimed tests that did not exist. They exist now.

### Changed expectations in existing tests (call out at handoff)

- `test_api.py::test_frontend_is_served_from_root` asserted the old page title
  "Arranger Workspace". It now asserts the new shell, that the page has no inline
  script, and that every module, legal page and the favicon are served.
- `test_api_security.py::test_static_frontend_assets_stay_cacheable` asserted
  `max-age` on `/app.js`. It now asserts that application modules and CSS are
  `no-cache` with a validator, for the reason above.
- `test_api_auth.py::test_production_delivery_failure_is_logged_counted_and_surfaced_on_ready`
  uses a local file store, because `/ready` now checks the file store and that
  test deliberately skips startup.

### Measured

- Deterministic arranging, 8 corpus pieces by 3 profiles after the planner
  change: 24 of 24 accepted, melodic recall 1.00 in all, 0.3 to 5.5 seconds each.
- Synthetic 10,080-note piece: planned in 14.9 s, arranged in 19.0 s, accepted,
  fidelity 0.907. Cost is linear in notes because regions are capped at 10.

### Remaining

Nothing in the backlog is unstarted. What is left is verification against real
external things, and decisions that are not engineering:

1. Run CI once on GitHub. Three jobs have never run anywhere: the Docker build
   and smoke test, the project journey on Postgres, and the browser job on
   Linux Chromium.
2. `python -m arranger_api.artifacts --check` against a real bucket before
   using `ARTIFACT_BACKEND=s3`.
3. One real model run (`MODEL_REPAIR_ENABLED=true`), which costs money and has
   not been authorised. Every branch is tested with fakes.
4. Measure transcription on real piano recordings (CC-licensed), not only
   synthesised audio.
5. `docs/legal-review.md`: operator identity, consent for hand measurements,
   minimum age, backup retention. These block `PUBLIC_LAUNCH=true`.
6. Rehearse restore and rollback once in staging.
7. Test with Firefox, Safari, a real phone and a screen reader.

## 2026-09-19 - Checkpoint 3: backend workflow complete

**Test state.** `pytest`: 783 passed, 1 skipped (Postgres integration; needs
`TEST_DATABASE_URL`). `ruff check .`: clean.

### Done since checkpoint 2

| Item | What exists | Evidence |
|---|---|---|
| D1, D2 | API hardening from the stopped agent, **reviewed by coverage, not line by line**: every one of the 21 brief items has a behaviour-named regression test; the dependency, error, app-factory and fixture modules were read in full | `test_api_security.py`, `test_api_auth.py`, `test_api_lifecycle.py`, `test_security.py`, `test_settings.py` |
| B6 | `explain.py`: transformation report built from the source/result diff, never from the plan's own claims; `estimate_difficulty` kept separate from findings | `test_workflows.py` |
| - | `selection.py`: melody track, ignored tracks, transposition, tempo/meter for recordings, per-note edits; applied as a new source revision | `test_workflows.py` |
| - | `application/workflows.py`: import by content sniffing, inspect, arrange, revise, export, playback events, findings language that never overclaims | `test_workflows.py` (40) |
| D3 | `artifacts.py`: `ArtifactStore` port; local (atomic writes, traversal-proof) and database adapters; S3 adapter with SigV4 (**not verified against a live bucket**) | `test_api_projects.py` |
| D4 | Migration 0008; `storage/workspace.py`: projects, append-only source and arrangement revisions pinning source, profile, plan, algorithm and model versions; atomic project creation | same |
| D5 | `jobs.py`: lease-based queue, progress, cooperative cancel, bounded retry with backoff, crash recovery, idempotency keys, per-kind time limits, retention sweep | same (incl. 4 threads racing for 3 jobs) |
| D6 | Routers `catalog`, `projects`, `account`: upload by raw body, inspect, source revisions, arrange/revise jobs, arrangement detail with playback data, MusicXML preview, MIDI/MusicXML/MXL/PDF export, compare, authenticated downloads, usage, account export, account deletion, presets, guided calibration, capability report, launch config for legal pages | `test_api_projects.py` (28) |
| - | Settings: artifact backend, workers, quotas, model limits, retention, operator details; production refuses `PUBLIC_LAUNCH=true` without them | `test_settings.py` (31) |

### Verified end to end over HTTP

Register, upload a MIDI file, inspect, correct the source, arrange with the
real engine, read the explanation, fetch the MusicXML preview, download MIDI
and compressed MusicXML and re-parse them, engrave and download a real PDF,
revise the plan into revision 2, compare revisions, search, rename, restart
the process on the same database and files, reopen, download again. Also: a
real WAV is transcribed by the Basic Pitch model into an editable project and
corrected. A second user gets 404 on all 20 probes of the first user's
records, including through their own project.

### Defects found by these tests and fixed

- The left-hand figure could land on the key the melody was holding (found as
  63 exported notes against 64 arranged). The renderer now keeps the left hand
  strictly under the sounding melody by octave displacement.
- `melody_fold_window` was centred on the unshifted melody, so a section moved
  down an octave to fit a small keyboard was folded straight back up.
- Project creation inserted the upload's artifact row before the project it
  references (foreign-key failure).
- A LIKE escape clause was mangled into an empty SQL string; the escape
  character is now `!`.
- With migrations left to a release step, job housekeeping crashed startup on a
  missing table. Workers now wait for a current schema.
- SMPTE MIDI returned 400 instead of 415.

### Remaining, in order

1. E1 frontend rebuild on these routes: upload, inspect and correct, profile
   presets and calibration, arrange with progress and cancel, notation preview
   with findings, playback with source/result compare, revisions, downloads,
   library, account. No `innerHTML` with data. WCAG 2.2 AA. Playwright journey
   and axe checks.
2. E3 legal pages from `/legal/config`.
3. Planner: `patterns_for_skill`, stride and broken-tenth candidates, bounded
   work on 10,000-note pieces.
4. E4: CI (incl. audio and LilyPond tests), Dockerfile with LilyPond and the
   audio extra, worker entry point, lockfile, runbooks, metrics for jobs.
5. Bring CLAUDE.md's "Not built yet" list and `docs/architecture.md` up to date.

## 2026-09-19 - Checkpoint 2: solver, fidelity v2, bounded repair loop

**Test state.** `pytest`: 709 passed, 1 skipped. The skip is
`tests/test_postgres_integration.py`, which needs `TEST_DATABASE_URL` and says
so. `ruff check .`: clean.

### Done since checkpoint 1

| Item | What exists | Evidence |
|---|---|---|
| C1 | `verify/solver.py` wired into `verify()`. Phrase search over onsets with held-note continuity, crossing, per-hand limits, pedal release, rolled chords, phrase reset. Outcomes FEASIBLE / INFEASIBLE / UNKNOWN; a cut-short search marks findings `certainty="unproven"`. INFEASIBLE is only claimed when no discarded branch could have done better | `test_solver.py` (33), `test_constraints.py` (19) |
| C1 | New STRAIN-only rules: `legato_break`, `hand_crossing`, `fast_repetition`, `finger_stretch`. None is HARD, per CLAUDE.md | same |
| C1 | `verify(..., staff_is_binding=False, assume_pedal=True)` for imported music. Arrangements we produce stay strictly bound to their staff | same |
| C3 | Chord fingering feasibility with finger availability, mirrored for the left hand | same |
| C4 | `fidelity.py` v2: melody matched against the right hand only, plus rhythm, contour, bass, harmony that needs the root from the accompaniment, accompaniment scaled by activity. Compares in source time so a slower arrangement is not scored as late | `test_fidelity.py` (23) |
| B5 | `engine.py` (evaluate, local repair, musical refinement) and a rewritten `agent.arrange`. The deterministic result is candidate zero; ties keep it. Stops on: accepted, attempts, time, cost, no improvement, repeated plan, provider failure, refusal, cancel. `model=None` is a supported deterministic mode | `test_agent.py` (41) |
| B5 | `ClaudeModel`: typed provider-error chain, refusal handling, automatic prompt caching across attempts, per-call timeout and max_tokens derived from the remaining budget, cost tracking. No `temperature` or `budget_tokens` (current models reject both) | unit-tested with fakes only; **no paid call was made** |
| B3+ | Plan-level `tempo_scale` (0.4 to 1.0): the last-resort lever for a right hand that is too fast for the player, since the melody may not be thinned | corpus run below |

### Measured on the public-domain corpus

**False HARD findings on human-written piano music** (octave-span profile, 20 pieces):

| Verifier | HARD findings |
|---|---|
| Old greedy assigner | 401 |
| Phrase solver, score read literally | 240 |
| Solver, staff treated as a hint | 152 |
| Solver, staff as hint, pedal assumed per bar | 21 |
| Same, advanced profile | 8 |

The 21 that remain are real for that hand: tenths in Brahms and wide Chopin
chords that an octave hand must roll. This is the corpus check CLAUDE.md asks
for before trusting HARD.

**Deterministic arranging, no model:** 8 pieces x 3 profiles = 24 runs, all 24
accepted (playable, fidelity at or above 0.88), 0.3 to 8 s each. Beginners on
fast pieces are accepted only with a reduced tempo (Fur Elise at 40%).

**Fidelity v2 calibration:** block chords 0.92 to 0.96, a bare pedal tone 0.89
to 0.92, no left hand 0.54, left hand deleted from half the piece 0.74, one
held note for the whole piece 0.54.

### Changed expectations in existing tests (call out at handoff)

- `test_constraints.py`: `test_lone_line_stays_in_one_hand_and_its_leaps_are_checked`
  became `test_a_free_hand_may_rescue_a_leap`. Its own comment called the old
  assertion a stopgap until the global solver existed. The failure it guarded
  against is now asserted directly by
  `test_a_lone_melodic_line_does_not_bounce_between_hands`.
- `test_agent.py`: two tests asserted `escalated` when the model never
  delivered a plan. With the deterministic draft as candidate zero that is no
  longer an escalation; they now assert the draft stands. Escalation is tested
  with a profile nothing can satisfy.

### Ownership change

The API hardening agent was stopped before it reported. Its work is in the tree
and the suite is green, but it has **not been reviewed**. Review
`src/arranger_api/` before building on it (next item).

### Remaining, in order

1. Review `src/arranger_api/` against the 21-item hardening brief; fix gaps.
2. Planner: use `patterns_for_skill`, add stride/broken-tenth candidates with
   tempo gates, bound work on 10,000-note pieces.
3. B6 transformation report and a difficulty estimate kept separate from
   findings, fidelity and import confidence.
4. D3-D6: artifact store, projects and revisions, job queue, import / arrange /
   export / account routers.
5. E1 frontend, E3 legal pages, E4 CI, Docker with LilyPond, runbooks, and
   bring CLAUDE.md's "Not built yet" list up to date.

## 2026-09-19 - Checkpoint: conversion layer done, engine and solver in progress

**How to run things.** Use the project venv, not the shell's default Python:
`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`. LilyPond 2.24.4 is
unpacked at `.cache/tools/lilypond-2.24.4/` (git-ignored) and is found
automatically; elsewhere set `LILYPOND_PATH` or put `lilypond` on `PATH`.

**Test state at this checkpoint.** 489 passed across domain and adapters.
2 failures in `tests/test_api_auth.py`, which belongs to the API hardening work
still in progress (see "In flight").

### Done and tested

| Item | What exists | Tests |
|---|---|---|
| A1 | `ir.Note` carries id, velocity, beat/beats, track, source_id, role, spelling, articulations, dynamic, rolled, confidence. `ir.Score` carries timeline, pedals, tracks, composer, source_format, warnings. `sounding_at` is indexed: planner went from 14 s to 1.5 s on a 2,000-note piece | existing suites |
| A2 | `timeline.py`: tempo, meter, key maps; exact beat/second/bar conversion; pickup bar is bar 1 | `test_midi_io.py`, `test_notation.py` |
| A3 | `io.read_midi_bytes`: limits, typed errors, velocity, tracks per (track, channel), CC64 pedal, key signature, tempo and meter maps, pickup detection, warnings. All 23 corpus files parse | `test_midi_io.py` (44) |
| A4 | `adapters/midi_writer.write_midi`; round trip preserves pitch, beats, tempo map, meter, key, pedal, pickup | same |
| A5 | `adapters/musicxml_reader` (.musicxml, .xml, .mxl): expat-level entity and DOCTYPE defences, zip limits, ties, tuplets, voices, staves, transposition, dynamics, articulations, pedal, pickup, fuzzed | `test_musicxml_reader.py` (104) |
| A6 | `notation.notate`: per-beat grid choice, bar-anchored windows, two voices per staff, ties over barlines, metric splitting, tuplets, rests, mode-aware spelling, key estimation, all tempo changes kept at their offset. Zero malformed bars on all 23 corpus pieces and across ten meters | `test_notation.py` (40) |
| A7 | `adapters/musicxml_writer` (`write_musicxml`, `write_mxl`); reader round trip is exact incl. tempo map and ids | reader + notation tests |
| A8 | `adapters/lilypond`: source writer plus sandboxed subprocess (no shell, scrubbed env, temp dir, timeout, cancellation, output cap, POSIX rlimits). Real PDF produced from a corpus piece in about 6 s | `test_lilypond.py` (15) |
| B1 | `analysis.extract_melody`: cheapest-path search with register floor, held-note awareness, track priors, user override via `role="melody"`. On the corpus, leaps over an octave fell from 21 to 0 (Mozart K545), 8 to 3 (L'Adieu), 5 to 0 (Brahms), with right-hand share equal or better everywhere | `test_engine.py` |
| B2 | `analysis.detect_chord_segments` (chord changes inside a bar, confidence gated), `bass_line`; bass bonus now scaled by template coverage so first inversions are not misread | `test_engine.py` |
| B3 | `render`: meter-aware pulses, pickup handling, `stride` and `broken_tenth`, `voicing` root/smooth, `bass` root/source, `harmonic_rhythm` bar/detected, silence respected, velocity carry-over, rolled flag, pedal spans, ids and provenance | `test_engine.py` (29), `test_render.py` |
| B4 | `plan`: strict typed `from_dict`, O(sections) `validate`, `validate_for_source` (coverage, range, skill ceiling from the left-hand-patterns skill) | `test_engine.py` |
| C2 | `profile`: per-hand span, finger availability, repeat rate, six presets with descriptions, `Calibration` and `profile_from_calibration` | not yet tested directly |
| E2 | `adapters/audio.transcribe_audio`: Basic Pitch ONNX, real inference, per-note confidence, limits, cancellation | `test_audio_transcription.py` (48) |

### Audio transcription: measured, on synthesised audio only

| Test | Precision | Recall | F1 |
|---|---|---|---|
| C major scale, monophonic | 1.000 | 1.000 | 1.000 |
| 45 s chromatic run | 1.000 | 1.000 | 1.000 |
| Two hands, melody over sustained triads | 0.757 | 1.000 | 0.862 |

Speed on this laptop, one CPU thread, no GPU: 1.5 to 1.7 s per minute of audio,
peak memory 217 to 224 MB on a 12 minute file. 48 tests, none skipped.
**Not measured:** any real recording. Every figure above is a ceiling. The
confidence score ranks notes usefully but 0.5 does not separate right from
wrong, so the correction step before arranging is required, not optional.
Unverified: the two-step install on a clean machine, Linux and the container,
and the audio tests are not yet in CI. Details: `docs/audio-transcription.md`.

### Written but NOT yet wired in or tested

- `src/arranger/verify/solver.py` (C1, C3): phrase DP over onsets with held
  notes, crossing, per-hand limits, pedal release, rolled chords, repetition,
  legato break, finger stretch, and FEASIBLE / INFEASIBLE / UNKNOWN. **Next
  step:** make `verify/constraints.check_hands` call `solve_hands`, exclude
  pedal-held notes from `check_total_polyphony`, set `Verdict.solver_status`
  and `Verdict.hands`, export the new names from `verify/__init__.py`, then
  run `python tests/test_constraints.py`.
- `tests/test_constraints.py::test_lone_line_stays_in_one_hand_and_its_leaps_are_checked`
  must change. Its own comment says its assertion is a stopgap for the greedy
  solver and that the global solver is the proper answer. Under the solver a
  genuine two-hand rescue is excused; keep the forced-same-hand test as the
  violating case and add a near-miss. Call this out at handoff.
- `verify/verdict.py` already has `Certainty`, `SolverStatus`, new STRAIN-only
  rules, `Violation.note_ids`, `Violation.certainty`.

### In flight (background agents, resumed after a session-limit interruption)

- API hardening (`src/arranger_api/**` except `routers/`): app factory,
  lifespan migrations, connection management, the 12 audit findings,
  argon2id with rehash, session-bound CSRF, email verification, production
  config validation, metrics. Do not edit those files until it reports.

### Remaining, in order

1. Wire and test the solver (above). Measure false-HARD reduction on the corpus
   against the greedy baseline recorded in the first entry of this log.
2. C4 fidelity v2: compare against the right-hand line only, add rhythm,
   contour, bass, harmony with third, accompaniment quality; abuse-case tests.
   Re-check `FIDELITY_FLOOR = 0.88` in `agent.py` and `planner.py`.
3. B5 repair loop: score the deterministic draft as candidate zero; bounds on
   time, output size, cost; repeated-plan and no-improvement stops; provider
   failure handling; deterministic local repair when no credentials. Load the
   `claude-api` skill before touching `ClaudeModel`.
4. Planner: use `patterns_for_skill`, offer voicing/bass/harmonic-rhythm
   candidates, bound work for long pieces.
5. B6 transformation report from the real source/result diff; difficulty
   estimate separate from findings.
6. D3-D6 backend: artifact store, projects and revisions, job queue, import /
   arrange / export / account endpoints as routers under
   `src/arranger_api/routers/` (register in `_ROUTER_MODULES`).
7. E1 frontend rebuild, E3 legal pages, E4 CI, Docker (install lilypond),
   runbooks. Update CLAUDE.md "Not built yet" so it stays truthful.

### Decisions worth remembering

- One representation, not two: `Score` gained optional notation context
  instead of a parallel model, so ids and provenance flow through render and
  verify unchanged.
- Browser notation preview will render the MusicXML export with a vendored
  JS engraver, so preview also exercises the export. PDF is LilyPond.
- Every new verifier rule is STRAIN, per CLAUDE.md. Only tighter limits on the
  existing HARD rules (per-hand span, finger count) are HARD.
- The engraver never sees user-supplied LilyPond. Source is generated from
  numbers plus two escaped strings.

## 2026-09-19 - Session start: inspection and baseline

**Environment.** The shell's active Python belonged to an unrelated project
venv without pytest or FastAPI. Created a project-local `.venv` (already in
`.gitignore`) and installed `.[api,dev]` plus `anthropic`.

**Baseline.** `pytest`: 129 passed, 1 failed
(`test_password_reset_email_failure_logs_status_and_body_without_leaking_secrets`).
`ruff`: 1 unused import. `tests/test_constraints.py`: 18/18.

**Corpus.** `fetch_corpus.py` downloaded and checksum-verified all 23
public-domain Mutopia pieces.

**Findings that drive the plan.**
- All 12 audit leads reproduce in the current code. Details and line numbers
  are in the D1 entry below once fixed.
- The logging test fails because `logging.basicConfig` is a no-op once pytest
  has installed root handlers, so the `arranger_api` logger stays at WARNING.
- Human-written piano pieces get many HARD findings from the greedy verifier
  (Satie Gymnopedie 66, Traumerei 49, Solace 210). These are false positives
  from per-onset assignment, no pedal model, and no rolls. This is the
  measurable target for C1.
- Planner cost is quadratic: 14 s for a 2,000-note piece, because
  `Score.sounding_at` scans every note at every onset for every candidate.
- The repair loop sends the deterministic draft to the model but never scores
  it, so a model can return something worse than the draft (B5).
- `ArrangementPlan.validate` loops over `range(start_bar, end_bar + 1)`, so a
  plan with `end_bar = 10**9` is a CPU and memory denial of service (B4).
- Frontend accepts only score JSON, exports only JSON, has stored-XSS sinks,
  mouse-only upload, focus loss on each keystroke, and stale-result races.

**Tooling available locally.** Node 22. Not installed: LilyPond, MuseScore,
ffmpeg, Docker, psql. `onnxruntime` has a Python 3.13 Windows wheel, so a real
Basic Pitch transcription path is feasible.
