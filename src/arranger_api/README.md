# Arranger API

Thin FastAPI interface over `arranger.application`.

## Install

```bash
pip install -e .[api]
```

## Run

```bash
arranger-api
```

Or:

```bash
python -m uvicorn arranger_api.main:app --reload
```

The API also serves the frontend from `/`, so local development can use:

```text
http://127.0.0.1:8000
```

## Endpoints

### The product: projects, revisions, jobs, files

These are what the frontend uses. All need a signed-in session; anything that
changes state also needs the `X-CSRF-Token` header and, when
`REQUIRE_VERIFIED_EMAIL` is on, a verified address.

| Method and path | What it does |
|---|---|
| `GET /catalog` | What this server can really do (PDF, audio, model), limits, profile presets, calibration steps. Public. |
| `POST /catalog/calibrate` | Turn guided hand measurements into a profile. Public, stateless. |
| `GET /legal/config` | Operator details and retention for the legal pages. Public. Blank when not configured. |
| `POST /projects/import?filename=&title=` | Upload a file as the raw request body. MIDI and MusicXML answer 201 with the project and its inspection. Audio answers 202 with a transcription job. The format is decided from the bytes. |
| `GET /projects?q=&limit=&offset=` | Your pieces, newest first, searchable. |
| `GET`, `PATCH`, `DELETE /projects/{id}` | One piece with its current source, revisions and active jobs; rename; delete with its files. |
| `POST /projects/{id}/sources` | Save corrections (melody part, ignored parts, transposition, tempo, meter, note edits) as a new source revision. |
| `GET /projects/{id}/sources/{sid}` and `.../musicxml` | A source revision with playback events; the source as notation. |
| `POST /projects/{id}/arrangements` | Start an arrangement job: 202 with the job. Body: `profile`, optional `source_id`, `plan` (evaluate an edited plan), `use_model`, `label`. Honours `Idempotency-Key`. |
| `GET`, `PATCH /projects/{id}/arrangements/{aid}` | An arrangement with plan, verdict, fidelity, report, difficulty, provenance, playback events and files; set its label. |
| `GET /projects/{id}/arrangements/{aid}/musicxml` | The notation the preview draws. The same bytes as the download. |
| `POST /projects/{id}/arrangements/{aid}/exports/{midi,musicxml,mxl,pdf}` | Make or reuse a file. PDF answers 202 with an engraving job, or 501 when LilyPond is not installed. |
| `GET /projects/{id}/compare?a=&b=` | What differs between two arrangements: findings, fidelity, difficulty, plan and hand profile. |
| `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry` | Progress, stage and result of background work. |
| `GET /artifacts/{id}/download` | Stream a file you own, as an attachment. |
| `GET /account/usage` | Where you stand against quotas, and the retention periods. |
| `GET /account/export` | A zip of every record and file held about you. |
| `DELETE /account` | Needs the password and `"confirm": "DELETE"`. Removes every row and file. |

Errors are `{"detail": {"error": "<code>", "detail": "<message>", "request_id": "..."}}`.
Codes are stable and the messages are safe to show.

### Accounts

`/auth/register`, `/auth/login`, `/auth/logout`, `/auth/logout-all`, `/auth/me`,
`/auth/sessions`, `/auth/password/change`, `/auth/password-reset/request` and
`/confirm`, `/auth/email/verify` and `/resend`.

### System

`GET /health` (alive), `GET /ready` (database, migrations, file store, email),
`GET /version`, `GET /metrics` (bearer token in production),
`GET /diagnostics/email`.

### Stateless compute and the original JSON records


Stateless:

- `GET /health`
- `GET /ready`
- `GET /version`
- `GET /diagnostics/email`
- `POST /verify`
- `POST /render`
- `POST /render-and-verify`
- `POST /plan/deterministic`
- `POST /plan/analysis`
- `POST /arrange/dry-run`

Auth:

- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/logout`
- `POST /auth/logout-all`
- `GET /auth/me`
- `POST /auth/password-reset/request`
- `POST /auth/password-reset/confirm`

Persistent:

- `POST /profiles`
- `GET /profiles`
- `GET /profiles/{profile_id}`
- `PUT /profiles/{profile_id}`
- `DELETE /profiles/{profile_id}`
- `POST /scores`
- `GET /scores`
- `GET /scores/{score_id}`
- `DELETE /scores/{score_id}`
- `POST /plans`
- `GET /plans`
- `GET /plans/{plan_id}`
- `PUT /plans/{plan_id}`
- `DELETE /plans/{plan_id}`
- `POST /arrangements/render-and-verify`
- `GET /arrangements`
- `GET /arrangements/{arrangement_id}`
- `GET /arrangements/{arrangement_id}/verdict`
- `POST /runs/dry-run`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /candidate-rankings`

The persistent endpoints require an authenticated session. The stateless
compute endpoints stay public because they do not read or write saved data.
Route handlers validate request data, convert it into domain objects, call the
application use cases, and return JSON. They should stay thin.

Persistent dry runs also save candidate-ranking rows. These rows record the
region features, candidate pattern, verifier cost, planner penalty, and chosen
label that a future ML ranker can train on.

## Storage

The API uses Postgres when `DATABASE_URL` or `ARRANGER_DATABASE_URL` is set to
a `postgres://` or `postgresql://` URL. Local runs fall back to SQLite at
`data/arranger.db` by default, and tests override storage with an in-memory
SQLite database.

Startup runs ordered migrations through `arranger_api.storage.migrations`.
Applied migration IDs are recorded in `schema_migrations`.

To run the real Postgres integration test:

```powershell
$env:POSTGRES_TEST_DATABASE_URL="postgresql://user:password@host:5432/database"
py tests\test_postgres_integration.py
```

Saved profiles, scores, plans, arrangements, and runs are owner-scoped by
`user_id`. A user cannot list, fetch, update, delete, render, or run another
user's saved records.

## Sessions

Registration and login set an HTTP-only `arranger_session` cookie. Session
tokens are stored as SHA-256 hashes. Passwords are hashed with Argon2id; older
PBKDF2 hashes still verify and are upgraded at the next sign-in. Cookies are
`Secure` in production.

Unsafe requests need an `X-CSRF-Token` header carrying the token bound to the
session, and an `Origin` on the allow-list. Rate limits are per address and per
user, in memory or in the database (`RATE_LIMIT_BACKEND`). Reset and
verification tokens are stored hashed, expire, work once, and are delivered in a
URL fragment. In production, set `EMAIL_PROVIDER=resend` and `RESEND_API_KEY`.

Request bodies are limited while streaming, not only by `Content-Length`.
Upload routes have their own cap (`MAX_UPLOAD_BYTES`). `docs/security.md` lists
every control and the test that pins it.

## Background jobs

Arranging, engraving and transcription run as jobs. `JOB_WORKERS` workers run
inside the web process (default 1). `python -m arranger_api.worker --workers N`
runs them in a separate process against the same database and file store; set
`JOB_WORKERS=0` on the web process when you do that. Jobs are claimed with a
lease, so workers never share a job and a dead worker's job is recovered.

## Environment

`.env.production.example` in the repository root is the complete, commented
list. `docs/deployment.md` explains the groups: identity and transport,
database, files, email, limits, retention, the optional model, operator details
for the legal pages, and observability. In development every setting has a
working default: SQLite at `data/arranger.db`, files under `data/artifacts/`,
email printed to the log.

See `docs/deployment.md` for Docker and Railway setup and `docs/runbook.md` for
operations.
