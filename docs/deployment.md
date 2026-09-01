# Deployment

Arranger now deploys as one FastAPI service that serves both the API and the
static frontend.

## Local Production Smoke Test

```bash
py -m pip install -e .[api]
$env:APP_ENV="production"
$env:HOST="127.0.0.1"
$env:PORT="8000"
$env:COOKIE_SECURE="false"
arranger-api
```

Open:

```text
http://127.0.0.1:8000
```

## Docker

```bash
docker build -t arranger .
docker run --rm -p 8000:8000 -e COOKIE_SECURE=false arranger
```

For local SQLite persistence during development:

```bash
docker run --rm -p 8000:8000 -e COOKIE_SECURE=false -e ARRANGER_DB_PATH=/data/arranger.db -v arranger-data:/data arranger
```

For cloud-like Postgres storage:

```bash
docker run --rm -p 8000:8000 -e DATABASE_URL=postgresql://user:password@host:5432/arranger arranger
```

## Railway

The repository includes `railway.toml` and a `Dockerfile`. Production storage
should use Railway Postgres or another managed Postgres provider.

1. Create a Railway project from the GitHub repository.
2. Add a Postgres database service.
3. Connect the Postgres service to the web service so Railway provides
   `DATABASE_URL`.
4. Railway will build with the Dockerfile.
5. Generate a public domain for the web service.
6. Set:

```text
FRONTEND_ORIGINS=https://your-generated-domain.up.railway.app
COOKIE_SECURE=true
APP_ENV=production
```

If `DATABASE_URL` is present and starts with `postgres://` or `postgresql://`,
the API opens Postgres and creates the required tables/indexes at startup.
Without `DATABASE_URL`, the API falls back to SQLite for local development.

## Real Postgres Integration Test

Use a disposable Postgres database or a Railway Postgres database that is safe
for test schemas. The test creates a temporary schema named
`arranger_test_<random>` and drops only that schema when it finishes.

PowerShell:

```powershell
$env:POSTGRES_TEST_DATABASE_URL="postgresql://user:password@host:5432/database"
py tests\test_postgres_integration.py
```

Do not commit real database URLs. Keep them in Railway variables, a local
`.env` file, or your shell session.

## Setting DATABASE_URL In Railway

If your Railway Postgres plugin exposes `DATABASE_URL` to the web service, no
manual copy is needed. If it does not:

1. Open the Railway project.
2. Add or select the Postgres database service.
3. Open the Postgres service variables.
4. Copy its public or private connection URL.
5. Open the Arranger web service variables.
6. Add `DATABASE_URL` with that connection string.
7. Redeploy the web service.
8. Confirm `GET /health` returns `{"status":"ok","service":"arranger-api"}`.

## Environment Variables

- `APP_ENV`: `development` or `production`.
- `HOST`: bind host. Use `0.0.0.0` in containers.
- `PORT`: bind port. Cloud hosts often set this automatically.
- `RELOAD`: set `false` in production.
- `COOKIE_SECURE`: set `true` on HTTPS.
- `SESSION_DAYS`: session lifetime.
- `FRONTEND_ORIGINS`: comma-separated CORS origins.
- `FRONTEND_DIR`: static frontend directory.
- `DATABASE_URL`: managed Postgres connection string for production.
- `ARRANGER_DATABASE_URL`: app-specific Postgres connection string override.
- `ARRANGER_DB_PATH`: SQLite database path for local development only.
