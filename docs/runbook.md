# Operations runbook

What to do, in order, when something needs doing. Deployment is in
`docs/deployment.md`; controls and their limits are in `docs/security.md`.

Nothing here has been rehearsed against the production deployment. Rehearse the
restore and the rollback once in staging before relying on them.

## What state exists, and where

| State | Where | Lost if it goes |
|---|---|---|
| Accounts, sessions, projects, source revisions, arrangements, jobs, rate-limit counters | The database (`DATABASE_URL`) | Everything. |
| Uploaded originals, MIDI, MusicXML and PDF files | The artifact store: the same database (`ARTIFACT_BACKEND=database`), a volume (`local`) or a bucket (`s3`) | Downloads of existing files. Notes, plans and arrangements are in the database, so MIDI, MusicXML and PDF can be made again from a saved arrangement; **uploaded originals cannot**. |
| Configuration and secrets | The host's variables | The ability to start. Keep a copy in a password manager. |

The application makes no backups. The container is stateless apart from `/data`.

## Backups

**Postgres with `ARTIFACT_BACKEND=database`.** One backup covers everything.

```bash
pg_dump --format=custom --no-owner --file=arranger-$(date +%F).dump "$DATABASE_URL"
```

Turn on the provider's daily snapshots as well if it has them. Keep at least 7
daily and 4 weekly copies, in a different account or region from the database.

**`ARTIFACT_BACKEND=local`.** Back up the volume at `ARTIFACT_DIR` at the same
time as the database. A database restored without its files shows artifacts that
answer 404; files without the database are unreachable.

**`ARTIFACT_BACKEND=s3`.** Turn on bucket versioning. Back up the database as
above.

**SQLite.** Do not copy the file while the app is running. Use:

```bash
sqlite3 /data/arranger.db ".backup '/backups/arranger-$(date +%F).db'"
```

Whatever retention you choose for backups, say it in the privacy notice: a
deleted account stays in backups until they expire. See `docs/legal-review.md`.

## Restore

1. Stop the web and worker services, so nothing writes during the restore.
2. Restore into a **new** database, not over the damaged one:
   ```bash
   createdb arranger_restore
   pg_restore --no-owner --dbname="$RESTORE_URL" arranger-2026-01-31.dump
   ```
3. Check it: `python -m arranger_api.storage.migrate --check` with `DATABASE_URL`
   pointing at the restore. If migrations are pending, the backup is older than
   the code; apply them with the same command without `--check`.
4. For `local` files, restore the volume from the same point in time.
5. Point `DATABASE_URL` at the restored database and start the web service.
6. `GET /ready` must answer 200. Sign in with a test account, open a project,
   download a file.
7. Jobs that were running when the backup was taken are recovered automatically
   once their lease expires (`JOB_LEASE_SECONDS`, 2 minutes by default), retried
   up to `JOB_MAX_ATTEMPTS`, and then marked failed with a message the user sees.
8. Keep the damaged database until you are sure. Then delete it.

## Rollback

Application versions can be rolled back freely because migrations only add.

1. Redeploy the previous image or commit from the host's deployment history.
2. `GET /version` must show the previous commit; `GET /ready` must answer 200.
3. Do **not** roll the database back. The older code ignores newer tables and
   columns. Restoring a backup to undo a deploy loses every user's work since.
4. Record what happened in `docs/changelog.md`.

If a future migration ever drops or renames something, it needs its own rollback
note in the pull request, and this section needs updating.

## Alerts worth having

`/metrics` is Prometheus text and needs `Authorization: Bearer $METRICS_TOKEN`
in production.

| Alert | Signal | First thing to check |
|---|---|---|
| Not ready | `GET /ready` not 200 for 2 minutes | The body says which: `database_unavailable`, `migrations_pending` or `artifact_store_unavailable`. |
| Server errors | `arranger_http_requests_total{status_class="5xx"}` above 1% of requests for 5 minutes | Logs for `"level": "ERROR"`; each has a `request_id` that also appears in the response the user saw. |
| Jobs failing | `arranger_jobs_finished_total{status="failed"}` rising | Log event `job_failed` carries the internal error; the user only sees a safe message. |
| Slow requests | `arranger_http_request_duration_seconds` p95 above 2 s | Are jobs running in the web process? Move them to a worker service (`JOB_WORKERS=0`). |
| Email failing | `arranger_email_send_total{outcome="failed"}` rising, or `/ready` reports email degraded | Resend dashboard; sender domain verification; `GET /diagnostics/email`. |
| Rate-limit store errors | `arranger_rate_limit_store_errors_total` rising | Database health. Limiting carries on per process while the shared store is failing, so limits are looser across instances until it recovers. |
| Sign-in attacks | `arranger_auth_failures_total` or `arranger_rate_limit_hits_total` spiking | Source addresses in the logs; block at the edge if it is one network. |
| Storage filling | Database or volume above 80% | `EXPORT_RETENTION_DAYS` makes generated files expire; they can be made again. |

Logs are JSON, one object per line, on stdout. Secrets are masked before they
are written; email addresses appear as domains only.

## Common situations

**A job is stuck at "running".** It is either still running (the limit is
`ARRANGE_MAX_SECONDS`, `ENGRAVE_MAX_SECONDS` or `TRANSCRIBE_MAX_SECONDS`) or its
worker died. A dead worker's job is picked up again after the lease expires. No
manual step is needed; the user can also cancel it.

**PDF export says it is unavailable.** LilyPond is not installed or not found.
`GET /catalog` shows `export_pdf.detail`. Set `LILYPOND_PATH` or rebuild the image
with `WITH_LILYPOND=1`.

**The notation preview will not load.** `frontend/vendor/verovio-toolkit-wasm.js`
is missing. Run `python fetch_vendor.py`; it verifies the file's checksum.

**A user cannot sign in after a password reset email never arrived.** Check
`arranger_email_send_total` and the Resend dashboard. Reset requests always answer
the same way, so the user cannot tell; the log and the metric can.

**A user asks for their data, or to be deleted.** Both are self-service on the
account page. If they cannot sign in, verify who they are through the email
address on the account before doing anything by hand, and prefer sending them a
password reset over touching the database.

**A copyright complaint arrives.** Uploads are private to the account, so there
is nothing public to take down. Deleting the project (or the account) removes the
files from live storage. Record the complaint and what was done. The process for
counter-notices is a legal question: see `docs/legal-review.md`.

## Secrets

| Secret | Rotate by |
|---|---|
| `DATABASE_URL` password | Change it at the provider, update the variable, redeploy. |
| `RESEND_API_KEY`, `ANTHROPIC_API_KEY`, `S3_SECRET_KEY` | Create a new key, update the variable, redeploy, delete the old key. |
| `METRICS_TOKEN` | Update the variable and the scraper together. |

There is no application-level signing secret: session and reset tokens are
random values stored as hashes, so there is nothing to rotate for them. To sign
every user out, delete the rows in `sessions`.

Rotate any secret that has appeared in a chat, log, screenshot, commit or CI
output. gitleaks runs in CI, but it only sees what is committed.

## Incident response

1. **Contain.** Rotate exposed credentials. If accounts may be affected, delete
   all rows in `sessions`, which signs everyone out.
2. **Preserve.** Export the logs for the period before they age out. Note request
   ids and timestamps.
3. **Fix or roll back.** Rolling back is usually faster; see above.
4. **Assess.** What data, whose, for how long. The tables in "What state exists"
   are the full list of what the service holds.
5. **Notify.** Whether and when users or a regulator must be told depends on the
   operator's jurisdiction and is not something this document can decide. See
   `docs/legal-review.md`.
6. **Record** the incident and the fix in `docs/changelog.md`.
