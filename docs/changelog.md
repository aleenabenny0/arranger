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
