# Deployment

Arranger deploys as one container image that can run two processes:

| Process | Command | What it does |
|---|---|---|
| Web | `arranger-api` | Serves the API and the static frontend. Also runs `JOB_WORKERS` background workers in-process (default 1). |
| Worker | `arranger-worker --workers N` | Runs arranging, engraving and transcription jobs only. Optional. |

A single web service is a complete deployment. Add a separate worker service when
jobs should not compete with requests for CPU; then set `JOB_WORKERS=0` on the web
service. Both processes must see the same database and the same artifact store.
Jobs are claimed with a lease, so any number of workers can run side by side and a
worker that dies has its job recovered after `JOB_LEASE_SECONDS`.

Day-two operations (backups, restore, rollback, alerts) are in `docs/runbook.md`.
Security controls and their limits are in `docs/security.md`.

## What the image contains

| Piece | How it gets there | If it is missing |
|---|---|---|
| Python dependencies | `pip install --constraint requirements.lock ".[api,model]"` | The build fails. |
| LilyPond | `apt-get install lilypond` (`--build-arg WITH_LILYPOND=0` to skip) | PDF export answers 501 and the UI hides the button. MIDI and MusicXML still work. |
| Audio transcription | `numpy onnxruntime soundfile soxr`, then `basic-pitch` with `--no-deps` (`--build-arg WITH_AUDIO=0` to skip) | Audio uploads answer 501 and the UI does not offer them. |
| Verovio notation engine | `python fetch_vendor.py`, SHA-256 pinned | The notation preview says it could not be loaded. Everything else works. |

`GET /catalog` reports which of these the running server really has. The CI
smoke test starts the built image and fails unless PDF export and audio import
both report as available.

The Dockerfile has not been built on the machine it was written on (no Docker
there). CI builds it and smoke-tests it on every push to `main`.

## Local run without Docker

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[api,model,dev]"
.venv\Scripts\python fetch_vendor.py
.venv\Scripts\arranger-api
```

Open `http://127.0.0.1:8000`. In development the app uses SQLite at
`data/arranger.db`, stores files under `data/artifacts`, prints emails to the log
instead of sending them, and does not require email verification.

Optional pieces:

- PDF: install LilyPond 2.24 and put it on `PATH`, or set `LILYPOND_PATH`.
- Audio: `pip install numpy onnxruntime soundfile soxr` then
  `pip install --no-deps basic-pitch`. See `docs/audio-transcription.md`.
- Model-assisted repair: set `ANTHROPIC_API_KEY` and `MODEL_REPAIR_ENABLED=true`.
  Without both, arranging is fully deterministic and makes no network calls.

## Docker

```bash
docker build -t arranger .

# Development-style run with SQLite and local files on a volume
docker run --rm -p 8000:8000 -v arranger-data:/data \
  -e APP_ENV=development -e COOKIE_SECURE=false arranger

# A separate worker against the same volume
docker run --rm -v arranger-data:/data -e APP_ENV=development arranger arranger-worker --workers 2
```

The container runs as the unprivileged user `arranger` (uid 10001). `/data` is the
only path it writes to.

## Production configuration

`.env.production.example` is the complete, commented list of settings. Startup
refuses to run in production when a required value is missing or unsafe, and
says which one. The groups that matter:

**Identity and transport.** `APP_ENV=production`, `APP_PUBLIC_URL` (https),
`FRONTEND_ORIGINS` (exact https origins), `COOKIE_SECURE=true`,
`CSRF_PROTECTION=true`, `TRUSTED_PROXY_COUNT` (how many proxies sit in front, so
the client address is read from the right hop).

**Database.** `DATABASE_URL` pointing at Postgres. SQLite in production is
refused unless `ALLOW_SQLITE_IN_PRODUCTION=true`, which is only sensible for a
single-instance deployment with a persistent volume.

**Files.** `ARTIFACT_BACKEND` is one of:

| Value | Where files live | Use when |
|---|---|---|
| `database` (production default) | A `BYTEA` table in the same database | One database is all you want to operate and back up. Fine up to a few GB. |
| `local` | `ARTIFACT_DIR`, which must be an absolute path on a persistent volume | Single instance with a volume. Web and worker must share the volume. |
| `s3` | Any S3-compatible bucket: `S3_ENDPOINT`, `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Many instances or large audio. **Not yet verified against a live bucket**: test it in staging first. |

Downloads are always authorised against the database and streamed by the API.
Storage keys and bucket URLs are never given to a browser.

**Email.** `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `PASSWORD_RESET_FROM` with a
sender on a domain you have verified. `REQUIRE_VERIFIED_EMAIL` defaults to true in
production: unverified accounts can sign in but cannot upload or arrange.

**Limits.** `MAX_UPLOAD_BYTES`, `QUOTA_PROJECTS`, `QUOTA_STORAGE_BYTES`,
`QUOTA_JOBS_PER_DAY`, `QUOTA_TRANSCRIPTIONS_PER_DAY`, `QUOTA_ACTIVE_JOBS`,
`ARRANGE_MAX_SECONDS`, `ENGRAVE_MAX_SECONDS`, `TRANSCRIBE_MAX_SECONDS`.

**Retention.** `UPLOAD_RETENTION_DAYS`, `EXPORT_RETENTION_DAYS`,
`JOB_RETENTION_DAYS`. The privacy notice displays these values, so changing them
changes what users are told. `0` means "kept until the user deletes it".

**Model.** `MODEL_REPAIR_ENABLED`, `ANTHROPIC_API_KEY`, `ARRANGER_MODEL`,
`MODEL_MAX_ATTEMPTS`, `MODEL_MAX_COST_USD`. Production refuses to enable the model
without a cost cap. When enabled, a text description of the piece and the
verifier's findings are sent to Anthropic; the uploaded file is not. The privacy
notice lists Anthropic as a recipient only when this is on.

**Operator details.** `OPERATOR_NAME`, `OPERATOR_POSTAL_ADDRESS`,
`OPERATOR_CONTACT_EMAIL`, `PRIVACY_CONTACT_EMAIL`, `DMCA_CONTACT_EMAIL`,
`OPERATOR_JURISDICTION`, `DATA_REGION`. The legal pages show "not provided" and a
draft banner until these are set. `PUBLIC_LAUNCH=true` removes the banner and is
refused at startup unless all of them except the postal address are set. Whether
a postal address must be published depends on where the operator is; see
`docs/legal-review.md` before setting `PUBLIC_LAUNCH`.

**Observability.** `LOG_LEVEL`, `METRICS_TOKEN` (required to read `/metrics` in
production), `COMMIT_SHA`.

## Migrations

Migrations are ordered, recorded in `schema_migrations`, and applied under a
lock (an advisory lock on Postgres, `BEGIN IMMEDIATE` on SQLite), so several
instances starting together apply each one exactly once.

```bash
python -m arranger_api.storage.migrate --check   # exit 1 if any are pending
python -m arranger_api.storage.migrate           # apply them
```

By default the web process applies pending migrations at startup. For a
controlled rollout set `RUN_MIGRATIONS_ON_STARTUP=false`, run the command above
as a release step, and then start the new version. `GET /ready` answers 503
`migrations_pending` until the schema is current, and background workers do not
start against an out-of-date schema.

Migrations are forward-only. Every one so far only adds tables, columns or
indexes, or fills in values that were empty. None drops or renames anything, so
the previous application version keeps working against the new
schema. That is what makes rollback safe; see the runbook.

## Railway

The repository includes `railway.toml`. The steps:

1. Create a project from the GitHub repository and add a Postgres service, so
   `DATABASE_URL` is provided to the web service.
2. Generate a public domain and set `APP_PUBLIC_URL` and `FRONTEND_ORIGINS` to it.
3. Set the secrets: `RESEND_API_KEY`, `METRICS_TOKEN`, and `ANTHROPIC_API_KEY` if
   the model is enabled.
4. Leave `ARTIFACT_BACKEND` unset (files go to Postgres) or configure `s3`.
   Do not use `local` on Railway without a volume: the filesystem is discarded
   on every deploy.
5. Deploy, then check the endpoints below.

For a separate worker, add a second service from the same repository with the
start command `arranger-worker --workers 2` and the same variables, and set
`JOB_WORKERS=0` on the web service.

## After every deploy

```text
GET /health    process is up
GET /ready     database reachable, migrations applied, artifact store healthy
GET /version   the commit that is running
GET /catalog   which capabilities this server really has
```

`/ready` is the one to alert on. The CI workflow `production-smoke` runs these
checks on demand.

## CI/CD

GitHub Actions runs on pull requests and on pushes to `main`:

| Job | What it proves |
|---|---|
| Python tests (3.12 and 3.13) | The whole suite, with LilyPond and the audio model installed so PDF and transcription tests are real, plus the verifier's standalone run and Ruff. |
| Browser journey and accessibility | Chromium drives upload, arrange, preview, playback, download, revise and reopen against a live server; axe-core checks every view; keyboard-only and phone-width runs. |
| Frontend checks | JavaScript syntax; no `innerHTML`, `eval` or inline script anywhere. |
| Security checks | `pip-audit` on the environment and on `requirements.lock`; gitleaks. |
| Postgres integration | Accounts, then the full project journey with files in `BYTEA` and four workers racing for three jobs, on a real Postgres. |
| Docker build and smoke test | The image builds, starts as non-root, is ready, and reports PDF and audio as available. |

`requirements.lock` is generated from a clean environment:

```bash
python -m venv /tmp/lock && /tmp/lock/bin/pip install ".[api,model]" numpy onnxruntime soundfile soxr
/tmp/lock/bin/pip install --no-deps basic-pitch
/tmp/lock/bin/pip freeze | grep -vi '^arranger' > requirements.lock   # keep the header comment
```

Regenerate it when `pyproject.toml` changes. It was last generated on Windows, so
Linux-only packages (uvloop) are not pinned by it.

See `docs/version-control.md` for branches, pull requests, tags and releases.
