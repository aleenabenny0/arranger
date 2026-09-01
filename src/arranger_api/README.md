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

Stateless:

- `GET /health`
- `GET /ready`
- `POST /verify`
- `POST /render`
- `POST /render-and-verify`
- `POST /arrange/dry-run`

Auth:

- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`

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

The persistent endpoints require an authenticated session. The stateless
compute endpoints stay public because they do not read or write saved data.
Route handlers validate request data, convert it into domain objects, call the
application use cases, and return JSON. They should stay thin.

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
tokens are stored as SHA-256 hashes, and passwords are stored with PBKDF2-SHA256
hashes. Local development cookies use `secure=False`; production HTTPS should
switch that to `secure=True`.

Unsafe cookie-authenticated requests require a matching `arranger_csrf` cookie
and `X-CSRF-Token` header. Auth and write traffic is rate-limited in process.
Password reset tokens are stored hashed and expire; production reset requests do
not expose raw tokens in the response.

Responses include defensive browser headers, including a restrictive content
security policy. Requests with a `Content-Length` larger than
`MAX_REQUEST_BYTES` are rejected before route handling.

## Environment

- `APP_ENV`: `development` by default. Use `production` in cloud hosting.
- `LOG_LEVEL`: Python log level, default `INFO`.
- `HOST`: bind host, default `127.0.0.1`.
- `PORT`: bind port, default `8000`. Cloud hosts usually provide this.
- `RELOAD`: enables Uvicorn reload. Defaults off in production.
- `COOKIE_SECURE`: secure auth cookies. Defaults on in production.
- `SESSION_DAYS`: session lifetime, default `30`.
- `MAX_SESSIONS_PER_USER`: active session cap per user, default `5`.
- `CSRF_PROTECTION`: enables CSRF checks, default `true`.
- `RATE_LIMIT_REQUESTS`: per-window request limit, default `120`.
- `RATE_LIMIT_WINDOW_SECONDS`: rate-limit window, default `60`.
- `MAX_REQUEST_BYTES`: maximum accepted request body size, default `1000000`.
- `PASSWORD_RESET_MINUTES`: reset token lifetime, default `30`.
- `FRONTEND_ORIGINS`: comma-separated CORS origins.
- `FRONTEND_DIR`: path to the static frontend, default `frontend/`.
- `DATABASE_URL`: managed Postgres connection string for production.
- `ARRANGER_DATABASE_URL`: app-specific Postgres connection string override.
- `ARRANGER_DB_PATH`: local SQLite path, default `data/arranger.db`.

See `docs/deployment.md` for Docker and Railway setup.
