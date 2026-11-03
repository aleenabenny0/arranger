# Changelog

## Unreleased

- Added production password reset email delivery through Resend.
- Added Railway email diagnostics and structured reset delivery logging.
- Added auth schema readiness checks and migration backfill protections.
- Added CI/CD, version-control, and security hardening tasks for the next release.
- Added a deterministic first-pass planning layer and API route.
- Improved deterministic section detection and per-section candidate scoring.
- Added region difficulty scoring and hardest-section ranking for the planner.
- Added musician-style note importance, local guidance retrieval, targeted repair
  suggestions, and ML-ready candidate ranking rows.
- Added persistent candidate-ranking storage and an authenticated retrieval API
  for future ML training data.
- Added MusicXML and compressed MusicXML import and export, a MIDI writer, and
  PDF engraving through a sandboxed LilyPond subprocess.
- Added audio transcription (Basic Pitch, one instrument) as a background job.
- Replaced greedy hand assignment with a phrase-based solver that reports
  FEASIBLE, INFEASIBLE or UNKNOWN, and models pedal, rolled chords and crossing.
- Added a deterministic arranging engine (planner, local repair, refinement);
  the model is now an optional, bounded improver on top of it.
- Added projects, append-only source and arrangement revisions, a lease-based
  job queue, an artifact store (local, database, S3), quotas, retention, account
  export and account deletion.
- Hardened the API: Argon2id, session-bound CSRF, email verification, streaming
  body limits, bounded schemas, database rate limits, production config checks.
- Rebuilt the frontend as an accessible static app with notation preview,
  playback, findings, revisions and downloads; added draft legal pages driven by
  launch configuration.
- Added browser, accessibility, artifact-store and Postgres-journey tests; a
  production Dockerfile; a dependency lock file; deployment, runbook, security
  and legal-review documents.
- Fixed: `argon2-cffi` missing from the `api` extra; a startup race when several
  processes first open a new SQLite file; hour-long caching of unfingerprinted
  frontend modules; a skip link that navigated instead of moving focus.
- One ordering for every comparison (`engine.quality_key`: cost, then tempo,
  then strain, then fidelity above the floor). The deterministic engine now
  tries every whole-piece figure itself before refining and eases strain in
  a plan that already plays, so it cannot lose to the brute-force baseline;
  the planner and the model loop rank candidates the same way, and the model
  is told the ordering, every plan lever, and exactly what a better plan
  must do. Engine version `2026.11.1`.
- `python -m arranger.evaluate` measures the engine and, when a model is
  named, the model loop against brute force on cost (the pre-registered
  protocol) and on quality; the scorecard carries a PASS / FAIL /
  INSUFFICIENT DATA verdict.
- `arranger-autotest`: the arranger tests itself on licensed music it finds
  on the Mutopia Project (public domain and permissive Creative Commons
  only), whole and in snippets, checking the readers, the round trips, the
  verifier, the melody rule and the engine against brute force for three
  players, against a committed baseline. A CI job runs it on the corpus and
  a weekly workflow on one catalogue page.
- Fixed: notation silently merged a repeated note that quantisation had put
  on its neighbour's position (two of Für Elise's 32nd-note E's); it now
  declares the merge in the export warnings. Found by the self-tester.
- Security: `certifi` pinned to 2026.10.19 in the lock file (PYSEC-2026-4320,
  a revoked root in the bundled CA store).
- The code is licensed under the MIT License (`LICENSE`). The corpus, the recorded
  Mutopia pages under `tests/fixtures/mutopia/` and the vendored Unity keep their
  own terms.
- Removed the unused `agent` extra (`langgraph`, `pydantic`); corrected the
  `verify/hands.py` docstring; `agent.MODEL_PRICES` knows the current models.
