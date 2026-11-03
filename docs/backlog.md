# Implementation backlog

Ordered by dependency. Each item lists its acceptance criterion. Status lives in
`docs/progress.md`; this file only changes when scope changes.

## A. Representation and conversion (domain, stdlib only)

| # | Item | Depends on | Accepted when |
|---|---|---|---|
| A1 | `Note`/`Score` carry stable ids, provenance, velocity, beat timing, tracks, pedals, warnings | - | Existing tests pass unchanged; new fields round-trip through score JSON |
| A2 | `Timeline`: tempo, meter, key maps; beat/second/bar conversion; pickup bar | - | Conversions are exact inverses across tempo and meter changes |
| A3 | MIDI reader from bytes with limits, tracks, velocity, CC64 pedal, key signature, warnings | A1 A2 | Malformed, empty, oversized, percussion-only and SMPTE files fail with typed errors; tempo/meter changes map to correct bars |
| A4 | MIDI writer | A1 A2 | write -> read preserves pitch, onset, duration, tempo map, pedal |
| A5 | MusicXML reader (.musicxml/.xml/.mxl) with XXE, entity and zip-bomb defences | A1 A2 | Attack payloads rejected; pitches, spelling, voices, staves, ties, tuplets, dynamics, articulations, pickup imported |
| A6 | Notation model: quantise, split at barlines, tie, fill rests, spell by key | A1 A2 | Every measure sums exactly to its meter; lossy steps emit warnings |
| A7 | MusicXML writer | A6 | Output re-imports with identical pitch/beat content; validates structurally |
| A8 | LilyPond source writer + sandboxed engraver subprocess | A6 | Real PDF produced (starts `%PDF`), timeout and output caps enforced, clear error when tool missing |

## B. Arrangement engine

| # | Item | Depends on | Accepted when |
|---|---|---|---|
| B1 | Melody identification: track scoring + continuity path; user override | A1 | Picks the tune on inner-voice and melody-rest cases where the skyline fails |
| B2 | Chord segments inside bars; bass line extraction | A2 | Half-bar chord changes detected; bass pitch class preserved in render |
| B3 | Meter-aware accompaniment, inversions and voice leading, dynamics carry-over | B2 | 3/4 and 6/8 produce correctly grouped left hand; total LH movement drops vs root position |
| B4 | Authoritative plan validation: types, coverage, overlap, ranges, resource caps | - | `end_bar=10**9` rejected in O(sections); wrong types give a typed error, never a 500 |
| B5 | Repair loop: deterministic draft is candidate zero; bounds on attempts, time, output size, cost; repeated-plan and no-improvement stops; provider failure handling; deterministic local repair without credentials | B4 | Model can never return worse than the draft; all bounds covered by tests |
| B6 | Transformation report from the actual source/result diff | B1 B2 | Explanations cite counted, real changes |

## C. Playability and quality

| # | Item | Depends on | Accepted when |
|---|---|---|---|
| C1 | Phrase-based exact DP hand solver: held notes, crossing, per-hand limits, release-based movement, pedal, rolls, repeated notes; FEASIBLE / INFEASIBLE / UNKNOWN | A1 | Fewer false HARD findings on human-written corpus pieces than greedy; timeout never reported as infeasible |
| C2 | Profile: separate left/right capacity, finger availability, presets, calibration | - | Near-miss and violating tests per rule |
| C3 | Chord fingering feasibility and line fingering suggestion (STRAIN only) | C1 C2 | Reduced-finger profiles change findings |
| C4 | Fidelity v2: melody, rhythm, contour, bass, harmony, accompaniment quality | B1 B2 | Degenerate outputs (no LH, one held note, transposed tune, LH covering melody) score below the floor |

## D. Backend

| # | Item | Depends on | Accepted when |
|---|---|---|---|
| D1 | Audit fixes (12 findings), argon2id with rehash-on-login, session-bound CSRF, email verification, prod config validation | - | One regression test per finding |
| D2 | Startup migrations with locking, connection management, transactions | - | No DDL in request path |
| D3 | Artifact store port + local and database adapters; authorised streaming; retention | - | Cross-user access returns 404 |
| D4 | Projects, source revisions, arrangement revisions with pinned source/profile/plan/algorithm/model versions | D2 D3 | Atomic create; reopen after restart |
| D5 | Job queue: arrange, engrave, transcribe; progress, cancel, retry, idempotency, quotas, lease recovery | D2 | Covered by tests incl. crash recovery |
| D6 | Import, arrange, export, account export/delete endpoints with typed contracts | A* B* C* D3-5 | Browser workflow runs on the real engine |

## E. Frontend, audio, legal, ops

| # | Item | Accepted when |
|---|---|---|
| E1 | Workspace UI: upload, inspect, profile, arrange, notation preview, playback compare, findings, revisions, downloads, library, account | Playwright journey + axe checks pass; no `innerHTML` with data |
| E2 | Audio transcription job (Basic Pitch ONNX) with confidence and correction | Synthesised-audio test recovers known notes, or exact missing prerequisite documented |
| E3 | Privacy, terms, cookies, support, upload-rights pages driven by launch config | No invented business facts |
| E4 | CI, lockfile, Docker runtime check, metrics, health, runbooks | Documented commands work from a clean checkout |
