# Architecture

Arranger is organized around one rule: model judgment and mechanical
correctness live in different layers. The model chooses an `ArrangementPlan`;
deterministic code renders that plan into a `Score`; the verifier decides
whether that score is playable for a specific `PlayerProfile`.

## Layers

```text
interfaces
  CLI and FastAPI today; workers later
    |
application
  use cases: verify_score, render_plan, fidelity_for, plan_score, arrange_score, baseline_for
    |
adapters
  JSON score debug format, MIDI input today; MusicXML/audio/storage later
    |
domain core
  ir, profile, plan, render, fidelity, verify
```

## Domain core

The domain core is the part to trust and protect:

- `arranger.ir`: `Note` and `Score`, the internal representation.
- `arranger.profile`: measurable player limits.
- `arranger.plan`: the only schema a model may emit.
- `arranger.planner`: deterministic first-pass arrangement planning.
- `arranger.render`: deterministic plan-to-score rendering.
- `arranger.verify`: dependency-free playability oracle.
- `arranger.fidelity`: checks that the arrangement is still recognizably the
  source music.

The verifier must remain dependency-free. It is the oracle every other layer
is judged against, so it should not break because a parser, model SDK, or web
framework changed.

## Application Layer

`arranger.application` is the stable orchestration surface for callers:

- `verify_score(score, profile)`
- `render_plan(plan, source)`
- `fidelity_for(source, arranged)`
- `plan_score(source, profile)`
- `arrange_score(source, profile, model=...)`
- `baseline_for(source, profile)`

CLIs, APIs, job workers, notebooks, and tests should prefer this layer instead
of manually wiring renderer, verifier, agent, and scoring code together.

## Adapters

Adapters translate external artifacts into domain objects and back:

| Adapter | Direction | Depends on |
|---|---|---|
| `arranger.io` (`read_midi_bytes`) and `adapters.midi` | MIDI in | stdlib |
| `adapters.midi_writer` | MIDI out | stdlib |
| `adapters.musicxml_reader` | MusicXML and compressed MusicXML in, with XML and zip defences | stdlib (expat, zipfile) |
| `adapters.musicxml_writer` | MusicXML and compressed MusicXML out | stdlib |
| `adapters.lilypond` | LilyPond source out; PDF through a sandboxed LilyPond subprocess | stdlib, plus the LilyPond program |
| `adapters.audio` | Recording in, through the Basic Pitch ONNX model | `numpy`, `onnxruntime`, `soundfile`, `soxr` |
| `adapters.score_json` | Stable debug and storage format, strict and bounded | stdlib |

`arranger.notation` sits between the domain and the two notation writers. It
turns seconds into bars, voices, ties, tuplets and spelled pitches once, so
MusicXML and the PDF cannot disagree.

Adapters may depend on third-party libraries. The domain core should not. Every
reader raises the typed errors in `arranger.limits` (`UnsupportedFormat`,
`MalformedFile`, `LimitExceeded`, `EmptyScore`, `UnsafeContent`), which the API
maps to status codes without string matching.

## Use cases

`arranger.application.workflows` is what every entry point calls:

- `import_bytes` sniffs the format from content and reads it.
- `inspect_source` reports tracks, key, meter, tempo, the likely melody part,
  difficulty, and how the source itself sits under an average hand.
- `arrange_source` applies the user's corrections (`arranger.selection`), runs
  the deterministic engine and, only if a model is passed, the bounded repair
  loop. It returns one `ArrangementBundle`: plan, score, verdict, fidelity,
  report, difficulty, provenance.
- `revise_arrangement` evaluates a user-edited plan the same way.
- `export_midi`, `export_musicxml`, `export_pdf`, `playback_events`.

The API layer adds nothing musical. `arranger_api.jobs` wraps these calls with
progress, cancellation, deadlines and retries; `arranger_api.routers` adds
ownership, quotas and persistence.

## Storage

`arranger_api.storage` is the persistence layer for the HTTP API. It uses
Postgres when `DATABASE_URL` or `ARRANGER_DATABASE_URL` is configured. Local
development falls back to SQLite. Schema changes are ordered migrations applied
under a lock (`storage/migrations.py`).

Two repositories share one connection and one transaction:

- `Storage` (`repositories.py`): users, sessions, tokens, and the original
  score/plan/arrangement records behind the JSON endpoints.
- `Workspace` (`workspace.py`): projects, source revisions, arrangement
  revisions, artifacts and jobs. Every query takes the user id, so ownership is
  enforced in one place and a wrong id is indistinguishable from a missing one.

Revisions are append-only. Correcting a source or re-arranging makes a new
revision; each arrangement records the source revision, hand profile, plan,
engine version and model it was made from.

### Transactions

`Storage.transaction()` is the one way to make several writes atomic: the
block commits once when it exits normally and rolls back on any exception;
nested blocks join the outermost one; and every repository write method
commits on its own only when no block is open. On SQLite a block starts with
`BEGIN IMMEDIATE`, so concurrent writers queue on `busy_timeout` instead of
failing with `SQLITE_BUSY`; on Postgres the driver's implicit transaction is
committed or rolled back when the block ends.

Every operation with more than one write runs inside one block.
`tests/test_transactions.py` forces a later write in each of them to fail and
checks, from a fresh connection, that nothing the earlier writes did was kept.
Those tests run on SQLite always and on Postgres in the CI Postgres job.

| Operation | Writes | Block |
|---|---|---|
| Register | revoke the presented session, insert the user, create the session | `auth_routes.register_endpoint` |
| Log in | upgrade the password hash, revoke the presented session, create the session | `auth_routes.login_endpoint` |
| Password reset | spend the token, set the password, spend sibling tokens, revoke sessions | `Storage.consume_password_reset_token` |
| Email verification | spend the token, mark the address verified, spend sibling tokens | `Storage.consume_email_verification_token` |
| Change password | set the password, revoke sessions | `Storage.change_password` |
| New session | revoke expired sessions, trim to the limit, insert | `Storage.create_session` |
| Dry run | insert the run, insert every candidate ranking | `main.create_dry_run_endpoint`, `Storage.create_candidate_rankings` |
| Housekeeping | delete dead sessions and spent tokens | `Storage.cleanup_expired` |
| Create project | insert the project, the upload artifact and the first source | `Workspace.create_project` |
| Correct a source | insert the revision, point the project at it | `Workspace.add_source_revision` |
| Save an arrangement | insert the revision, update the project, insert the MIDI and MusicXML file rows | `Workspace.add_arrangement`, `jobs.run_arrange` |
| Finish a transcription | insert the project and source, link the upload to it | `jobs.run_transcribe` |
| Queue, cancel, retry a job | read the row, then write it | `Workspace.create_job`, `request_cancel`, `retry_job` |
| Recover lost jobs | fail the exhausted ones, requeue the rest | `Workspace.recover_expired_jobs` |
| Delete a project | jobs, artifacts, arrangements, sources, the project | `Workspace.delete_project` |
| Delete an account | every table, then the user | `Workspace.delete_user` |

File bytes live outside the database. Where a block also stores files
(`jobs.run_arrange`, the import routes), a rollback is followed by deleting
the bytes that were already written, so no file is left that no row points to.

`GET /arrangements/export` streams a user's saved arrangements as NDJSON in
batches of 100: on Postgres through a named server-side cursor, on SQLite by
iterating the statement. The stream holds its own connection for its lifetime.

Files live behind the `ArtifactStore` protocol (`artifacts.py`): a local
directory, a database table, or an S3-compatible bucket. Rows in `artifacts`
hold the metadata and the storage key; downloads are authorised against that row
and streamed by the API. See `docs/artifact-storage.md`.

Jobs are rows in `jobs`, claimed with a lease by `JobRunner` (`jobs.py`), which
runs inside the web process, in `python -m arranger_api.worker`, or both. A job
is idempotent by key, cancellable, bounded in time, retried after a crash, and
recovered when its worker dies.

Local runs use `data/arranger.db` and `data/artifacts/`. Tests use throwaway
SQLite files. Cloud runs should use managed Postgres.

## Auth And Permissions

`arranger_api.main` serves the web app (React and TypeScript, built by Vite into
`frontend-react/dist`) from `/` when that directory is present. The app is
typed against the API's OpenAPI document, so a route change that is not
reflected in `frontend-react/openapi.json` fails the web app's type check. `arranger_api.settings` reads cloud configuration from
environment variables such as `PORT`, `FRONTEND_ORIGINS`, `COOKIE_SECURE`, and
`FRONTEND_DIR`.

`arranger_api.auth` owns browser authentication. Registration and login create
HTTP-only cookie sessions. Persistent records carry `user_id`, and repository
methods require that user id for saved-resource reads and writes.

The stateless compute endpoints are public. Persistent endpoints are private:
profiles, scores, plans, arrangements, and runs can only be accessed by their
owner.

Unsafe cookie-authenticated writes require a double-submit CSRF token. Auth and
write paths are rate-limited in process. Password reset tokens are stored only
as hashes and are consumed once, revoking active sessions for that user. Reset
delivery goes through an email provider port so production can use Resend while
local development can log reset links without external services.

## Ports

`arranger.ports` names infrastructure-facing protocols. Today it defines the
model and score-reader boundaries. As the project grows, add ports before
binding the core directly to databases, queues, HTTP frameworks, or model
vendors.

## Dependency Direction

Allowed direction:

```text
interfaces -> application -> adapters/domain -> domain
```

Avoid these:

- verifier importing model clients or web frameworks
- renderer reading files directly
- model clients emitting notes, MusicXML, or MIDI events
- APIs duplicating orchestration that belongs in `arranger.application`

## Next Architectural Milestones

1. Per-note fingering inside `arranger.verify.solver`, still dependency-free.
2. Score planner candidates on their own region instead of re-rendering the
   whole piece (`arranger.planner`).
3. Read arpeggio and grace marks from MusicXML into the IR, to remove the last
   false HARD findings on human-written scores.
4. Verify the S3 artifact store against a real bucket; consider signed,
   short-lived download URLs once it is.
5. Source separation and beat tracking in front of `adapters.audio`.
6. Convert the repair loop to LangGraph only if it grows branches a plain loop
   cannot express. It has not needed to.

Done since this list was first written: MusicXML and PDF as adapters, the global
hand solver, the artifact store, and database-backed rate limiting (which
replaced the planned Redis).

See `docs/artifact-storage.md` for the artifact storage decision,
`docs/algorithm.md` for how the musical decisions are made, and
`docs/deployment.md` for how it runs.
