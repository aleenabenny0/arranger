"""Projects, revisions, artifacts and the job queue.

Every read and write here takes a `user_id` and puts it in the WHERE clause.
There is no method that fetches one of these rows by id alone, so a router
cannot forget the ownership check: another user's id simply finds nothing.

Revisions are append-only. Correcting a source or re-arranging a piece adds a
row; it never edits one. That is what lets an old arrangement keep pointing at
exactly the source, profile and plan it was made from.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .repositories import Storage, format_timestamp, new_id, utc_now

JOB_ACTIVE = ("queued", "running")
JOB_TERMINAL = ("succeeded", "failed", "cancelled")

_JSON_COLUMNS = (
    "profile_json", "score_json", "selection_json", "inspection_json", "plan_json",
    "arranged_json", "verdict_json", "summary_json", "report_json", "payload_json", "result_json",
)
_BOOL_COLUMNS = ("accepted", "cancel_requested")


def _row(row: Any) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for column in _JSON_COLUMNS:
        if column in data:
            raw = data.pop(column)
            data[column.removesuffix("_json")] = json.loads(raw) if raw is not None else None
    for column in _BOOL_COLUMNS:
        if column in data:
            data[column] = bool(data[column])
    return data


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _like(term: str) -> str:
    """A LIKE pattern matching `term` literally. `!` is the escape character:
    a backslash means different things to Python, SQLite and Postgres."""
    escaped = term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


class Workspace:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.conn = storage.conn

    # --- projects ----------------------------------------------------------

    def create_project(
        self, user_id: str, *, title: str, composer: str, kind: str, filename: str,
        score: dict, note_count: int, bar_count: int, inspection: dict | None,
        upload: dict | None = None,
    ) -> dict:
        """A project and its first source revision, together or not at all."""
        project_id, source_id, now = new_id(), new_id(), utc_now()
        with self.storage.transaction():
            # Order matters: the artifact row references the project.
            self.conn.execute(
                """
                INSERT INTO projects (id, user_id, title, composer, source_kind, status,
                                      current_source_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (project_id, user_id, title, composer, kind, source_id, now, now),
            )
            artifact_id = None
            if upload is not None:
                artifact_id = self._insert_artifact(
                    user_id, project_id=project_id, source_id=source_id, arrangement_id=None,
                    now=now, **upload,
                )
            self.conn.execute(
                """
                INSERT INTO project_sources (id, project_id, user_id, revision, kind, filename,
                                             note_count, bar_count, score_json, selection_json,
                                             inspection_json, upload_artifact_id, created_at)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (source_id, project_id, user_id, kind, filename, note_count, bar_count,
                 _dump(score), _dump(inspection), artifact_id, now),
            )
        return self.get_project(user_id, project_id)

    def get_project(self, user_id: str, project_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ? AND status != 'deleted'",
            (project_id, user_id),
        ).fetchone()
        return _row(row)

    def list_projects(self, user_id: str, *, query: str = "", limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        where = "p.user_id = ? AND p.status != 'deleted'"
        params: list[Any] = [user_id]
        if query:
            where += " AND (LOWER(p.title) LIKE ? ESCAPE '!' OR LOWER(p.composer) LIKE ? ESCAPE '!')"
            params += [_like(query.lower())] * 2
        total = self.conn.execute(f"SELECT COUNT(*) AS n FROM projects p WHERE {where}", tuple(params)).fetchone()["n"]  # nosec B608 - built only from literal fragments; every value is a bound parameter
        rows = self.conn.execute(
            f"""
            SELECT p.*,
                   (SELECT COUNT(*) FROM project_arrangements a WHERE a.project_id = p.id) AS arrangement_count,
                   (SELECT note_count FROM project_sources s WHERE s.id = p.current_source_id) AS note_count,
                   (SELECT bar_count FROM project_sources s WHERE s.id = p.current_source_id) AS bar_count
            FROM projects p WHERE {where}
            ORDER BY p.updated_at DESC LIMIT ? OFFSET ?
            """,  # nosec B608 - built only from literal fragments; every value is a bound parameter
            (*params, limit, offset),
        ).fetchall()
        return [_row(r) for r in rows], int(total)

    def count_projects(self, user_id: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE user_id = ? AND status != 'deleted'", (user_id,)
        ).fetchone()["n"])

    def update_project(self, user_id: str, project_id: str, *, title: str | None = None,
                       composer: str | None = None, profile: dict | None = None) -> dict | None:
        fields, params = ["updated_at = ?"], [utc_now()]
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if composer is not None:
            fields.append("composer = ?")
            params.append(composer)
        if profile is not None:
            fields.append("profile_json = ?")
            params.append(_dump(profile))
        cursor = self.conn.execute(
            f"UPDATE projects SET {', '.join(fields)} WHERE id = ? AND user_id = ? AND status != 'deleted'",  # nosec B608 - built only from literal fragments; every value is a bound parameter
            (*params, project_id, user_id),
        )
        self.storage._commit()
        return self.get_project(user_id, project_id) if cursor.rowcount else None

    def delete_project(self, user_id: str, project_id: str) -> list[str] | None:
        """Delete a project and everything under it. Returns the storage keys to remove."""
        with self.storage.transaction():
            if self.get_project(user_id, project_id) is None:
                return None
            keys = [r["storage_key"] for r in self.conn.execute(
                "SELECT storage_key FROM artifacts WHERE project_id = ? AND user_id = ?", (project_id, user_id)
            ).fetchall()]
            self.conn.execute("DELETE FROM jobs WHERE project_id = ? AND user_id = ?", (project_id, user_id))
            self.conn.execute("DELETE FROM artifacts WHERE project_id = ? AND user_id = ?", (project_id, user_id))
            self.conn.execute("DELETE FROM project_arrangements WHERE project_id = ? AND user_id = ?", (project_id, user_id))
            self.conn.execute("DELETE FROM project_sources WHERE project_id = ? AND user_id = ?", (project_id, user_id))
            self.conn.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))
        return keys

    # --- source revisions ------------------------------------------------------

    def _next_revision(self, table: str, project_id: str) -> int:
        row = self.conn.execute(
            f"SELECT COALESCE(MAX(revision), 0) + 1 AS n FROM {table} WHERE project_id = ?", (project_id,)  # nosec B608 - table is an internal constant, never user input; values are bound parameters
        ).fetchone()
        return int(row["n"])

    def add_source_revision(
        self, user_id: str, project_id: str, *, based_on: str, selection: dict | None,
        inspection: dict | None, score: dict | None = None, note_count: int | None = None,
        bar_count: int | None = None,
    ) -> dict | None:
        """A new revision of a project's source: corrected, never overwritten."""
        with self.storage.transaction():
            previous = self.get_source(user_id, based_on)
            if previous is None or previous["project_id"] != project_id:
                return None
            source_id, now = new_id(), utc_now()
            revision = self._next_revision("project_sources", project_id)
            self.conn.execute(
                """
                INSERT INTO project_sources (id, project_id, user_id, revision, kind, filename,
                                             note_count, bar_count, score_json, selection_json,
                                             inspection_json, upload_artifact_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_id, project_id, user_id, revision, previous["kind"], previous["filename"],
                 previous["note_count"] if note_count is None else note_count,
                 previous["bar_count"] if bar_count is None else bar_count,
                 _dump(previous["score"] if score is None else score), _dump(selection),
                 _dump(inspection), previous["upload_artifact_id"], now),
            )
            self.conn.execute(
                "UPDATE projects SET current_source_id = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (source_id, now, project_id, user_id),
            )
        return self.get_source(user_id, source_id)

    def get_source(self, user_id: str, source_id: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM project_sources WHERE id = ? AND user_id = ?", (source_id, user_id)
        ).fetchone())

    def list_sources(self, user_id: str, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, project_id, revision, kind, filename, note_count, bar_count, selection_json, created_at
            FROM project_sources WHERE project_id = ? AND user_id = ? ORDER BY revision DESC
            """,
            (project_id, user_id),
        ).fetchall()
        return [_row(r) for r in rows]

    # --- arrangement revisions ---------------------------------------------------

    def add_arrangement(
        self, user_id: str, project_id: str, source_id: str, *, origin: str, accepted: bool,
        n_hard: int, n_strain: int, fidelity_score: float, difficulty: float, algorithm_version: str,
        model: str | None, profile: dict, plan: dict, arranged: dict, verdict: dict, summary: dict,
        report: dict, label: str = "",
    ) -> dict | None:
        with self.storage.transaction():
            source = self.get_source(user_id, source_id)
            if source is None or source["project_id"] != project_id:
                return None
            arrangement_id, now = new_id(), utc_now()
            revision = self._next_revision("project_arrangements", project_id)
            self.conn.execute(
                """
                INSERT INTO project_arrangements (
                    id, project_id, user_id, revision, source_id, label, origin, accepted, n_hard,
                    n_strain, fidelity_score, difficulty, algorithm_version, model, profile_json,
                    plan_json, arranged_json, verdict_json, summary_json, report_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (arrangement_id, project_id, user_id, revision, source_id, label, origin, int(accepted),
                 n_hard, n_strain, fidelity_score, difficulty, algorithm_version, model, _dump(profile),
                 _dump(plan), _dump(arranged), _dump(verdict), _dump(summary), _dump(report), now),
            )
            self.conn.execute(
                "UPDATE projects SET current_arrangement_id = ?, profile_json = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (arrangement_id, _dump(profile), now, project_id, user_id),
            )
        return self.get_arrangement(user_id, arrangement_id)

    def get_arrangement(self, user_id: str, arrangement_id: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM project_arrangements WHERE id = ? AND user_id = ?", (arrangement_id, user_id)
        ).fetchone())

    def list_arrangements(self, user_id: str, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, project_id, revision, source_id, label, origin, accepted, n_hard, n_strain,
                   fidelity_score, difficulty, algorithm_version, model, summary_json, created_at
            FROM project_arrangements WHERE project_id = ? AND user_id = ? ORDER BY revision DESC
            """,
            (project_id, user_id),
        ).fetchall()
        return [_row(r) for r in rows]

    def label_arrangement(self, user_id: str, arrangement_id: str, label: str) -> dict | None:
        cursor = self.conn.execute(
            "UPDATE project_arrangements SET label = ? WHERE id = ? AND user_id = ?",
            (label, arrangement_id, user_id),
        )
        self.storage._commit()
        return self.get_arrangement(user_id, arrangement_id) if cursor.rowcount else None

    # --- artifacts ------------------------------------------------------------------

    def _insert_artifact(
        self, user_id: str, *, project_id: str | None, source_id: str | None, arrangement_id: str | None,
        kind: str, filename: str, content_type: str, storage_key: str, size_bytes: int, sha256: str,
        now: str | None = None, expires_at: str | None = None,
    ) -> str:
        artifact_id = new_id()
        self.conn.execute(
            """
            INSERT INTO artifacts (id, user_id, project_id, arrangement_id, source_id, kind, filename,
                                   content_type, storage_key, size_bytes, sha256, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, user_id, project_id, arrangement_id, source_id, kind, filename, content_type,
             storage_key, size_bytes, sha256, now or utc_now(), expires_at),
        )
        return artifact_id

    def create_artifact(self, user_id: str, **fields: Any) -> dict:
        artifact_id = self._insert_artifact(user_id, **fields)
        self.storage._commit()
        return self.get_artifact(user_id, artifact_id)

    def get_artifact(self, user_id: str, artifact_id: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id)
        ).fetchone())

    def find_artifact(self, user_id: str, arrangement_id: str, kind: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM artifacts WHERE user_id = ? AND arrangement_id = ? AND kind = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, arrangement_id, kind),
        ).fetchone())

    def list_artifacts(self, user_id: str, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, project_id, arrangement_id, source_id, kind, filename, content_type, size_bytes, "
            "created_at, expires_at FROM artifacts WHERE user_id = ? AND project_id = ? ORDER BY created_at DESC",
            (user_id, project_id),
        ).fetchall()
        return [_row(r) for r in rows]

    def storage_used(self, user_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM artifacts WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["n"])

    def expired_artifacts(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, user_id, storage_key FROM artifacts WHERE expires_at IS NOT NULL AND expires_at <= ? LIMIT ?",
            (utc_now(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_artifact_row(self, artifact_id: str) -> None:
        self.conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        self.storage._commit()

    # --- jobs ------------------------------------------------------------------------

    def create_job(
        self, user_id: str, *, kind: str, payload: dict, project_id: str | None = None,
        idempotency_key: str | None = None, max_attempts: int = 2,
    ) -> tuple[dict, bool]:
        """Queue a job. Returns (job, created). The same key returns the same job."""
        with self.storage.transaction():
            if idempotency_key:
                existing = self.conn.execute(
                    "SELECT * FROM jobs WHERE user_id = ? AND idempotency_key = ?", (user_id, idempotency_key)
                ).fetchone()
                if existing is not None:
                    return _row(existing), False
            job_id, now = new_id(), utc_now()
            self.conn.execute(
                """
                INSERT INTO jobs (id, user_id, project_id, kind, status, progress, stage, payload_json,
                                  attempts, max_attempts, idempotency_key, run_after, created_at)
                VALUES (?, ?, ?, ?, 'queued', 0, 'Waiting to start', ?, 0, ?, ?, ?, ?)
                """,
                (job_id, user_id, project_id, kind, _dump(payload), max_attempts, idempotency_key, now, now),
            )
        return self.get_job(user_id, job_id), True

    def job_for_key(self, user_id: str, idempotency_key: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND idempotency_key = ?", (user_id, idempotency_key)
        ).fetchone())

    def get_job(self, user_id: str, job_id: str) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone())

    def list_jobs(self, user_id: str, *, project_id: str | None = None, active_only: bool = False, limit: int = 50) -> list[dict]:
        where, params = "user_id = ?", [user_id]
        if project_id:
            where += " AND project_id = ?"
            params.append(project_id)
        if active_only:
            where += " AND status IN ('queued', 'running')"
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC LIMIT ?", (*params, limit)  # nosec B608 - built only from literal fragments; every value is a bound parameter
        ).fetchall()
        return [_row(r) for r in rows]

    def count_active_jobs(self, user_id: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND status IN ('queued', 'running')", (user_id,)
        ).fetchone()["n"])

    def count_jobs_since(self, user_id: str, since: datetime, kind: str | None = None) -> int:
        where, params = "user_id = ? AND created_at >= ?", [user_id, format_timestamp(since)]
        if kind:
            where += " AND kind = ?"
            params.append(kind)
        return int(self.conn.execute(f"SELECT COUNT(*) AS n FROM jobs WHERE {where}", tuple(params)).fetchone()["n"])  # nosec B608 - built only from literal fragments; every value is a bound parameter

    def request_cancel(self, user_id: str, job_id: str) -> dict | None:
        """Queued jobs are cancelled at once. Running jobs are asked to stop."""
        with self.storage.transaction():
            job = self.get_job(user_id, job_id)
            if job is None:
                return None
            now = utc_now()
            if job["status"] == "queued":
                self.conn.execute(
                    "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, finished_at = ?, "
                    "stage = 'Cancelled' WHERE id = ? AND status = 'queued'",
                    (now, job_id),
                )
            elif job["status"] == "running":
                self.conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        return self.get_job(user_id, job_id)

    def retry_job(self, user_id: str, job_id: str) -> dict | None:
        with self.storage.transaction():
            job = self.get_job(user_id, job_id)
            if job is None or job["status"] not in ("failed", "cancelled"):
                return None
            self.conn.execute(
                """
                UPDATE jobs SET status = 'queued', progress = 0, stage = 'Waiting to start', attempts = 0,
                       cancel_requested = 0, error_code = NULL, error_public = NULL, error_internal = NULL,
                       result_json = NULL, lease_owner = NULL, lease_expires_at = NULL, run_after = ?,
                       started_at = NULL, finished_at = NULL
                WHERE id = ? AND user_id = ?
                """,
                (utc_now(), job_id, user_id),
            )
        return self.get_job(user_id, job_id)

    # Worker side. These are not user-scoped: a worker serves every user.

    def claim_job(self, worker_id: str, lease_seconds: int, kinds: tuple[str, ...] | None = None) -> dict | None:
        """Atomically take the oldest runnable job, or None.

        The UPDATE carries the `status = 'queued'` guard, so two workers that
        picked the same candidate cannot both win: one of them updates zero rows
        and tries again.
        """
        now_dt = datetime.now(timezone.utc)
        now = format_timestamp(now_dt)
        lease = format_timestamp(now_dt + timedelta(seconds=lease_seconds))
        where, params = "status = 'queued' AND run_after <= ?", [now]
        if kinds:
            where += f" AND kind IN ({', '.join('?' for _ in kinds)})"
            params += list(kinds)
        for _ in range(5):
            row = self.conn.execute(
                f"SELECT id FROM jobs WHERE {where} ORDER BY created_at LIMIT 1", tuple(params)  # nosec B608 - built only from literal fragments; every value is a bound parameter
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return None
            cursor = self.conn.execute(
                """
                UPDATE jobs SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                       attempts = attempts + 1, started_at = COALESCE(started_at, ?), stage = 'Starting'
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, lease, now, row["id"]),
            )
            self.conn.commit()
            if cursor.rowcount == 1:
                return _row(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
        return None

    def claim_job_by_id(self, job_id: str, worker_id: str, lease_seconds: int) -> dict | None:
        """Take one specific queued job: the one a queue message names.

        One conditional UPDATE decides it. None means the job is not queued any
        more (running elsewhere, finished, cancelled, or unknown), which is how
        a duplicate delivery of the same message is recognised and dropped.
        """
        now_dt = datetime.now(timezone.utc)
        now = format_timestamp(now_dt)
        lease = format_timestamp(now_dt + timedelta(seconds=lease_seconds))
        with self.storage.transaction():
            cursor = self.conn.execute(
                """
                UPDATE jobs SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                       attempts = attempts + 1, started_at = COALESCE(started_at, ?), stage = 'Starting'
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, lease, now, job_id),
            )
            if cursor.rowcount != 1:
                return None
        return _row(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def set_aside_job(self, job_id: str, reason: str) -> bool:
        """Fail a job whose queue message is being dead-lettered, if it is still open."""
        with self.storage.transaction():
            cursor = self.conn.execute(
                """
                UPDATE jobs SET status = 'failed', error_code = 'dead_lettered',
                       error_public = 'This job failed repeatedly and was set aside. Try again later.',
                       error_internal = ?, finished_at = ?, lease_owner = NULL, lease_expires_at = NULL,
                       stage = 'Failed'
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (reason[:4000], utc_now(), job_id),
            )
        return cursor.rowcount == 1

    def mark_dispatched(self, job_id: str, message_id: str) -> None:
        """Record that the job's queue message was sent, and which one."""
        with self.storage.transaction():
            self.conn.execute(
                "UPDATE jobs SET dispatched_at = ?, queue_message_id = ? WHERE id = ?",
                (utc_now(), message_id[:200], job_id),
            )

    def heartbeat(self, job_id: str, worker_id: str, *, progress: float, stage: str, lease_seconds: int) -> bool:
        """Record progress and extend the lease. Returns True if the job should stop."""
        lease = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=lease_seconds))
        self.conn.execute(
            "UPDATE jobs SET progress = ?, stage = ?, lease_expires_at = ? "
            "WHERE id = ? AND lease_owner = ? AND status = 'running'",
            (max(0.0, min(1.0, progress)), stage[:200], lease, job_id, worker_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT cancel_requested, status, lease_owner FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return True   # the project was deleted underneath the job
        return bool(row["cancel_requested"]) or row["status"] != "running" or row["lease_owner"] != worker_id

    def finish_job(self, job_id: str, worker_id: str, *, status: str, result: dict | None = None,
                   error_code: str | None = None, error_public: str | None = None,
                   error_internal: str | None = None, stage: str = "") -> None:
        self.conn.execute(
            """
            UPDATE jobs SET status = ?, result_json = ?, error_code = ?, error_public = ?, error_internal = ?,
                   progress = CASE WHEN ? = 'succeeded' THEN 1 ELSE progress END, stage = ?,
                   finished_at = ?, lease_owner = NULL, lease_expires_at = NULL
            WHERE id = ? AND lease_owner = ?
            """,
            (status, _dump(result), error_code, error_public, (error_internal or "")[:4000] or None,
             status, stage[:200], utc_now(), job_id, worker_id),
        )
        self.conn.commit()

    def release_for_retry(self, job_id: str, worker_id: str, *, delay_seconds: int, error_internal: str) -> None:
        run_after = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
        self.conn.execute(
            "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, run_after = ?, "
            "stage = 'Waiting to retry', error_internal = ? WHERE id = ? AND lease_owner = ?",
            (run_after, error_internal[:4000], job_id, worker_id),
        )
        self.conn.commit()

    def recover_expired_jobs(self) -> dict[str, int]:
        """Jobs whose worker died mid-run: requeue them, or fail them if out of attempts.

        The two updates are one decision about the same set of rows. Applied
        separately, a failure between them would fail the exhausted jobs and
        leave the others stranded as `running` until the next sweep.
        """
        now = utc_now()
        with self.storage.transaction():
            failed = self.conn.execute(
                """
                UPDATE jobs SET status = 'failed', error_code = 'worker_lost',
                       error_public = 'This job stopped unexpectedly and could not be retried.',
                       finished_at = ?, lease_owner = NULL, lease_expires_at = NULL, stage = 'Failed'
                WHERE status = 'running' AND lease_expires_at <= ? AND attempts >= max_attempts
                """,
                (now, now),
            ).rowcount
            requeued = self.conn.execute(
                "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, "
                "stage = 'Waiting to retry' WHERE status = 'running' AND lease_expires_at <= ?",
                (now,),
            ).rowcount
        return {"requeued": requeued, "failed": failed}

    def prune_finished_jobs(self, older_than_days: int) -> int:
        cutoff = format_timestamp(datetime.now(timezone.utc) - timedelta(days=older_than_days))
        count = self.conn.execute(
            "DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'cancelled') AND finished_at <= ?", (cutoff,)
        ).rowcount
        self.conn.commit()
        return count

    # --- account ------------------------------------------------------------------------

    def export_user(self, user_id: str) -> dict:
        """Everything stored about one user, as plain data. Secrets are left out."""
        def rows(sql: str) -> list[dict]:
            return [_row(r) for r in self.conn.execute(sql, (user_id,)).fetchall()]

        user = self.conn.execute(
            "SELECT id, email, display_name, created_at, updated_at, email_verified_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return {
            "user": dict(user) if user else None,
            "sessions": rows("SELECT id, ip_address, user_agent, created_at, last_seen_at, expires_at, revoked_at "
                             "FROM sessions WHERE user_id = ?"),
            "saved_profiles": rows("SELECT * FROM profiles WHERE user_id = ?"),
            "projects": rows("SELECT * FROM projects WHERE user_id = ?"),
            "project_sources": rows("SELECT * FROM project_sources WHERE user_id = ?"),
            "project_arrangements": rows("SELECT * FROM project_arrangements WHERE user_id = ?"),
            "artifacts": rows("SELECT id, project_id, arrangement_id, source_id, kind, filename, content_type, "
                              "size_bytes, sha256, created_at, expires_at FROM artifacts WHERE user_id = ?"),
            "jobs": rows("SELECT id, project_id, kind, status, attempts, created_at, started_at, finished_at, "
                         "error_code FROM jobs WHERE user_id = ?"),
            "legacy_scores": rows("SELECT * FROM scores WHERE user_id = ?"),
            "legacy_plans": rows("SELECT * FROM plans WHERE user_id = ?"),
            "legacy_arrangements": rows("SELECT * FROM arrangements WHERE user_id = ?"),
            "legacy_runs": rows("SELECT * FROM runs WHERE user_id = ?"),
            "feedback": rows("SELECT * FROM candidate_feedback WHERE user_id = ?"),
        }

    def artifact_keys_for_user(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, storage_key, filename, kind, project_id FROM artifacts WHERE user_id = ?", (user_id,)
        ).fetchall()]

    def delete_user(self, user_id: str) -> list[str]:
        """Remove the account and every row that belongs to it. Returns storage keys to delete."""
        with self.storage.transaction():
            keys = [a["storage_key"] for a in self.artifact_keys_for_user(user_id)]
            for table in ("jobs", "artifacts", "project_arrangements", "project_sources", "projects",
                          "candidate_feedback", "candidate_rankings", "runs", "arrangements", "plans",
                          "scores", "profiles", "password_reset_tokens", "email_verification_tokens", "sessions"):
                self.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))  # nosec B608 - table is an internal constant, never user input; values are bound parameters
            self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return keys
