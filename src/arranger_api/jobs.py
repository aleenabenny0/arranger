"""Background jobs: arranging, engraving, transcribing.

The queue is the `jobs` table. A worker claims a job by flipping it from
`queued` to `running` under a lease; it renews the lease every time it reports
progress. If the worker dies, the lease runs out and another worker (in this
process or on another host) picks the job up again, until `max_attempts`.

Workers hold a database connection only for the instant they need one. A
three-minute arrangement must not sit on a pooled connection for three minutes.

What a user may be told about a failure is decided here, once:
- a `ScoreImportError` carries its own vetted public message;
- a `JobFailed` is raised by handlers with a message written for users;
- anything else becomes "Something went wrong", and the traceback goes to the
  log and to `error_internal`, which no route returns.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from arranger.adapters.score_json import score_from_dict, score_to_dict
from arranger.agent import ClaudeModel, RepairBudget, model_credentials_available
from arranger.application import workflows
from arranger.engine import Cancelled
from arranger.limits import ScoreImportError
from arranger.plan import ArrangementPlan
from arranger.profile import PlayerProfile
from arranger.selection import SelectionError, SourceSelection

from .artifacts import CONTENT_TYPES, ArtifactStore, new_storage_key, safe_filename, sha256_hex
from .observability import get_logger, log_event
from .storage import Database, Storage
from .storage.repositories import format_timestamp
from .storage.workspace import Workspace

logger = get_logger("arranger_api.jobs")

JOB_KINDS = ("arrange", "engrave", "transcribe")
HEARTBEAT_SECONDS = 0.5


class JobFailed(Exception):
    """A failure with a message fit for the user. `retryable` asks for another attempt."""

    def __init__(self, code: str, public: str, *, retryable: bool = False):
        super().__init__(public)
        self.code, self.public, self.retryable = code, public, retryable


@dataclass
class JobServices:
    settings: Any
    database: Database
    artifacts: ArtifactStore
    metrics: Any = None
    engraver: Any = None          # injectable for tests
    transcriber: Callable | None = None
    model_factory: Callable | None = None
    queue: Any = None             # a JobQueue, or None when workers poll the table


def dispatch(queue: Any, ws: Workspace, job: dict) -> dict:
    """Send the message that tells a worker the job exists, when a queue is in use.

    The row is committed before this runs, so a worker that receives the
    message finds a queued job to claim. Called only for a job that was just
    created: a repeated request with the same idempotency key must not send a
    second message for a job that is already on its way.
    """
    if queue is None or job.get("status") != "queued":
        return job
    message_id = queue.send(job["id"])
    ws.mark_dispatched(job["id"], message_id)
    return job


class JobContext:
    def __init__(self, job: dict, services: JobServices, worker_id: str, deadline: float):
        self.job, self.services, self.worker_id, self.deadline = job, services, worker_id, deadline
        self.payload: dict = job["payload"] or {}
        self.user_id: str = job["user_id"]
        self._stop = False
        self._last_beat = 0.0
        self._progress = (0.0, "Starting")

    @contextmanager
    def workspace(self) -> Iterator[Workspace]:
        database = self.services.database
        with database.connection() as conn:
            yield Workspace(Storage(conn, dialect=database.dialect))

    def progress(self, fraction: float, stage: str) -> None:
        self._progress = (fraction, stage)
        now = time.monotonic()
        if now - self._last_beat < HEARTBEAT_SECONDS and fraction < 1.0:
            return
        self._last_beat = now
        with self.workspace() as ws:
            if ws.heartbeat(self.job["id"], self.worker_id, progress=fraction, stage=stage,
                            lease_seconds=self.services.settings.job_lease_seconds):
                self._stop = True

    def should_cancel(self) -> bool:
        if self._stop:
            return True
        if time.monotonic() > self.deadline:
            raise JobFailed("time_limit", "This took longer than the time allowed and was stopped.")
        if time.monotonic() - self._last_beat >= HEARTBEAT_SECONDS:
            self.progress(*self._progress)
        return self._stop

    def put_file(self, ws: Workspace, *, kind: str, data: bytes) -> tuple[str, str]:
        """Write bytes to the object store. Returns (storage key, content type).

        Call it outside any database transaction: with `ARTIFACT_BACKEND=database`
        the store writes through a connection of its own, and on SQLite that
        write would wait on the lock an open `BEGIN IMMEDIATE` holds.
        """
        settings = self.services.settings
        if ws.storage_used(self.user_id) + len(data) > settings.quota_storage_bytes:
            raise JobFailed("storage_quota", "Your storage is full. Delete a project to make room.")
        key = new_storage_key(self.user_id, kind)
        content_type = CONTENT_TYPES[kind][0]
        self.services.artifacts.put(key, data, content_type)
        return key, content_type

    def record_file(self, ws: Workspace, *, key: str, content_type: str, kind: str, data: bytes, filename: str,
                    project_id: str | None, arrangement_id: str | None = None, source_id: str | None = None,
                    retention_days: int = 0) -> dict:
        """The row that makes stored bytes findable. Safe inside a transaction."""
        expires = None
        if retention_days > 0:
            from datetime import datetime, timedelta, timezone

            expires = format_timestamp(datetime.now(timezone.utc) + timedelta(days=retention_days))
        return ws.create_artifact(
            self.user_id, project_id=project_id, arrangement_id=arrangement_id, source_id=source_id,
            kind=kind, filename=filename, content_type=content_type, storage_key=key,
            size_bytes=len(data), sha256=sha256_hex(data), expires_at=expires,
        )

    def discard_files(self, keys: list[str]) -> None:
        """Delete stored bytes whose rows were rolled back. A leak is logged, never raised."""
        for key in keys:
            try:
                self.services.artifacts.delete(key)
            except Exception as exc:
                logger.warning("artifact_cleanup_failed", exc_info=exc, extra={"storage_key": key})

    def store(self, ws: Workspace, *, kind: str, data: bytes, filename: str, project_id: str | None,
              arrangement_id: str | None = None, source_id: str | None = None,
              retention_days: int = 0) -> dict:
        """Bytes first, then the row; never leaves bytes nobody can find."""
        key, content_type = self.put_file(ws, kind=kind, data=data)
        try:
            return self.record_file(
                ws, key=key, content_type=content_type, kind=kind, data=data, filename=filename,
                project_id=project_id, arrangement_id=arrangement_id, source_id=source_id,
                retention_days=retention_days,
            )
        except Exception:
            self.services.artifacts.delete(key)
            raise


# --- handlers ------------------------------------------------------------------------


def _model_for(ctx: JobContext):
    settings = ctx.services.settings
    if not (ctx.payload.get("use_model") and settings.model_repair_enabled):
        return None
    if ctx.services.model_factory is not None:
        return ctx.services.model_factory()
    if not model_credentials_available():
        return None
    return ClaudeModel(settings.model_name or None, timeout=min(120.0, settings.arrange_max_seconds))


def run_arrange(ctx: JobContext) -> dict:
    settings = ctx.services.settings
    payload = ctx.payload
    with ctx.workspace() as ws:
        source = ws.get_source(ctx.user_id, payload["source_id"])
        project = ws.get_project(ctx.user_id, payload["project_id"])
    if source is None or project is None or source["project_id"] != project["id"]:
        raise JobFailed("not_found", "That project or source no longer exists.")
    try:
        profile = PlayerProfile.from_dict(payload["profile"])
        score = score_from_dict(source["score"])
        selection = SourceSelection.from_dict(source["selection"])
        if payload.get("plan") is not None:
            ctx.progress(0.2, "Checking your changes")
            bundle = workflows.revise_arrangement(score, profile, ArrangementPlan.from_dict(payload["plan"]), selection)
        else:
            budget = RepairBudget(
                max_attempts=settings.model_max_attempts,
                max_seconds=max(5.0, ctx.deadline - time.monotonic() - 5.0),
                max_cost_usd=settings.model_max_cost_usd,
                skip_model_when_draft_accepted=True,
            )
            bundle = workflows.arrange_source(
                score, profile, selection, model=_model_for(ctx), budget=budget,
                progress=lambda f, stage: ctx.progress(0.05 + 0.8 * f, stage),
                should_cancel=ctx.should_cancel,
            )
    except (SelectionError, ValueError) as exc:
        if isinstance(exc, ScoreImportError):
            raise
        raise JobFailed("invalid_input", str(exc)[:300]) from exc

    ctx.progress(0.9, "Saving the arrangement")
    title = project["title"]
    midi = workflows.export_midi(bundle.arranged)
    musicxml, export_warnings = workflows.export_musicxml(bundle.arranged)
    summary = bundle.summary()
    summary["export_warnings"] = export_warnings
    report = bundle.report.to_dict()
    report["attempts"] = [
        {k: a.get(k) for k in ("number", "hard", "strain", "cost", "error", "origin")} for a in bundle.attempts
    ]
    # The arrangement row and its two file rows are one result: a revision
    # without its downloads, or downloads pointing at no revision, is a bug
    # the user would see. The bytes live outside the database and go in
    # first, outside the transaction (see `put_file`); if the rows roll back,
    # the bytes are deleted again.
    files: list[tuple[str, str, str, bytes, str]] = []
    with ctx.workspace() as ws:
        for kind, data, suffix in (("midi", midi, ".mid"), ("musicxml", musicxml, ".musicxml")):
            try:
                key, content_type = ctx.put_file(ws, kind=kind, data=data)
            except BaseException:
                ctx.discard_files([f[0] for f in files])
                raise
            files.append((key, content_type, kind, data, suffix))
    try:
        with ctx.workspace() as ws, ws.storage.transaction():
            arrangement = ws.add_arrangement(
                ctx.user_id, project["id"], source["id"], origin=bundle.origin, accepted=bundle.accepted,
                n_hard=len(bundle.verdict.hard), n_strain=len(bundle.verdict.strain),
                fidelity_score=bundle.fidelity["score"], difficulty=bundle.difficulty.level,
                algorithm_version=bundle.algorithm_version, model=bundle.model, profile=profile.to_dict(),
                plan=json.loads(bundle.plan.to_json()), arranged=score_to_dict(bundle.arranged),
                verdict=workflows.verdict_to_dict(bundle.verdict), summary=summary, report=report,
                label=str(payload.get("label") or "")[:120],
            )
            if arrangement is None:
                raise JobFailed("not_found", "That project no longer exists.")
            base = safe_filename(f"{title} - arrangement {arrangement['revision']}", "arrangement")
            for key, content_type, kind, data, suffix in files:
                ctx.record_file(
                    ws, key=key, content_type=content_type, kind=kind, data=data, filename=f"{base}{suffix}",
                    project_id=project["id"], arrangement_id=arrangement["id"],
                    retention_days=settings.export_retention_days,
                )
    except BaseException:
        ctx.discard_files([f[0] for f in files])
        raise
    return {"arrangement_id": arrangement["id"], "revision": arrangement["revision"], "accepted": bundle.accepted}


def run_engrave(ctx: JobContext) -> dict:
    payload = ctx.payload
    with ctx.workspace() as ws:
        arrangement = ws.get_arrangement(ctx.user_id, payload["arrangement_id"])
        if arrangement is None:
            raise JobFailed("not_found", "That arrangement no longer exists.")
        existing = ws.find_artifact(ctx.user_id, arrangement["id"], "pdf")
        project = ws.get_project(ctx.user_id, arrangement["project_id"])
    if existing is not None and ctx.services.artifacts.exists(existing["storage_key"]):
        return {"artifact_id": existing["id"], "reused": True}
    ctx.progress(0.1, "Preparing the score")
    score = score_from_dict(arrangement["arranged"])
    score.title = (project or {}).get("title") or score.title
    score.composer = (project or {}).get("composer") or score.composer
    ctx.progress(0.25, "Engraving the PDF")
    pdf, warnings = workflows.export_pdf(
        score, paper=payload.get("paper", "a4"), should_cancel=ctx.should_cancel, engraver=ctx.services.engraver
    )
    ctx.progress(0.9, "Saving the PDF")
    base = safe_filename(f"{score.title} - arrangement {arrangement['revision']}", "arrangement")
    with ctx.workspace() as ws:
        artifact = ctx.store(ws, kind="pdf", data=pdf, filename=f"{base}.pdf", project_id=arrangement["project_id"],
                             arrangement_id=arrangement["id"],
                             retention_days=ctx.services.settings.export_retention_days)
    return {"artifact_id": artifact["id"], "reused": False, "warnings": warnings[:20]}


def run_transcribe(ctx: JobContext) -> dict:
    payload = ctx.payload
    with ctx.workspace() as ws:
        upload = ws.get_artifact(ctx.user_id, payload["upload_artifact_id"])
    if upload is None:
        raise JobFailed("not_found", "The uploaded recording is no longer available.")
    data = ctx.services.artifacts.get(upload["storage_key"])
    transcribe = ctx.services.transcriber
    if transcribe is None:
        from arranger.adapters.audio import TranscriptionCancelled, transcribe_audio

        def transcribe(data, filename, progress, should_cancel):   # noqa: ANN001
            try:
                return transcribe_audio(data, filename=filename, progress=progress, should_cancel=should_cancel)
            except TranscriptionCancelled:
                raise Cancelled() from None

    result = transcribe(
        data, upload["filename"], lambda f, stage: ctx.progress(0.05 + 0.85 * f, stage), ctx.should_cancel
    )
    score = result.score
    score.title = str(payload.get("title") or score.title or "Recording")[:200]
    ctx.progress(0.92, "Checking the transcription")
    inspection = workflows.inspect_source(score)
    inspection["transcription"] = {
        "model": result.model, "overall_confidence": round(result.overall_confidence, 3),
        "tempo_bpm": result.tempo_bpm, "tempo_confidence": result.tempo_confidence,
        "warnings": list(result.warnings),
        "note": "Transcribed by a model from audio. Expect wrong, missing and extra notes, "
                "especially in dense chords. Check it before arranging.",
    }
    # The new project and the upload that now belongs to it are one change.
    with ctx.workspace() as ws, ws.storage.transaction():
        project = ws.create_project(
            ctx.user_id, title=score.title, composer="", kind="audio", filename=upload["filename"],
            score=score_to_dict(score), note_count=len(score.notes), bar_count=score.last_bar(),
            inspection=inspection,
        )
        ws.conn.execute("UPDATE artifacts SET project_id = ? WHERE id = ? AND user_id = ?",
                        (project["id"], upload["id"], ctx.user_id))
    return {"project_id": project["id"], "note_count": len(score.notes),
            "overall_confidence": round(result.overall_confidence, 3)}


HANDLERS: dict[str, Callable[[JobContext], dict]] = {
    "arrange": run_arrange, "engrave": run_engrave, "transcribe": run_transcribe,
}


# --- the runner ---------------------------------------------------------------------------


class JobRunner:
    def __init__(self, services: JobServices, handlers: dict[str, Callable] | None = None):
        self.services = services
        self.handlers = handlers or HANDLERS
        self.worker_id = f"w-{uuid.uuid4().hex[:12]}"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _limit_for(self, kind: str) -> float:
        settings = self.services.settings
        return float({"arrange": settings.arrange_max_seconds, "engrave": settings.engrave_max_seconds,
                      "transcribe": settings.transcribe_max_seconds}.get(kind, 120))

    def run_once(self, worker_id: str | None = None) -> dict | None:
        """Claim and run one job. Returns the claimed job, or None if the queue was empty."""
        worker_id = worker_id or self.worker_id
        database = self.services.database
        with database.connection() as conn:
            job = Workspace(Storage(conn, dialect=database.dialect)).claim_job(
                worker_id, self.services.settings.job_lease_seconds, tuple(self.handlers)
            )
        if job is None:
            return None
        self._execute(job, worker_id)
        return job

    def run_job(self, job_id: str, worker_id: str | None = None) -> str:
        """Run the job a queue message names.

        Returns the outcome: `succeeded`, `failed`, `cancelled`, `retry` (a
        later attempt is wanted), or `skipped` when the job was not queued
        any more, which is what a duplicate delivery of the same message, a
        cancelled job or an already finished one all look like. Skipping is
        what makes at-least-once delivery safe: a message may arrive twice,
        the job runs once.
        """
        worker_id = worker_id or self.worker_id
        database = self.services.database
        with database.connection() as conn:
            job = Workspace(Storage(conn, dialect=database.dialect)).claim_job_by_id(
                job_id, worker_id, self.services.settings.job_lease_seconds
            )
        if job is None:
            return "skipped"
        return self._execute(job, worker_id)

    def _execute(self, job: dict, worker_id: str) -> str:
        started = time.monotonic()
        ctx = JobContext(job, self.services, worker_id, started + self._limit_for(job["kind"]))
        status, fields = "succeeded", {}
        try:
            result = self.handlers[job["kind"]](ctx)
            fields = {"result": result, "stage": "Done"}
        except Cancelled:
            status, fields = "cancelled", {"stage": "Cancelled"}
        except JobFailed as exc:
            if exc.retryable and job["attempts"] < job["max_attempts"]:
                self._retry(job, worker_id, str(exc))
                return "retry"
            status = "failed"
            fields = {"error_code": exc.code, "error_public": exc.public, "stage": "Failed"}
        except ScoreImportError as exc:
            cancelled = type(exc).__name__ in ("EngravingCancelled",)
            status = "cancelled" if cancelled else "failed"
            fields = {"stage": "Cancelled"} if cancelled else {
                "error_code": exc.code, "error_public": exc.public, "error_internal": exc.detail, "stage": "Failed"}
        except Exception as exc:
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
            logger.error("job_failed", exc_info=exc, extra={"job_id": job["id"], "kind": job["kind"]})
            if job["attempts"] < job["max_attempts"]:
                self._retry(job, worker_id, detail)
                return "retry"
            status = "failed"
            fields = {"error_code": "internal_error", "error_public": "Something went wrong while processing this.",
                      "error_internal": detail, "stage": "Failed"}
        # A job that finishes before it notices a cancel request stays succeeded:
        # the work is done and saved, and throwing it away helps nobody.
        database = self.services.database
        with database.connection() as conn:
            Workspace(Storage(conn, dialect=database.dialect)).finish_job(job["id"], worker_id, status=status, **fields)
        elapsed = time.monotonic() - started
        log_event(logger, "job_finished", job_id=job["id"], kind=job["kind"], status=status,
                  seconds=round(elapsed, 3), attempt=job["attempts"])
        metrics = self.services.metrics
        if metrics is not None and hasattr(metrics, "jobs_finished"):
            metrics.jobs_finished.inc(kind=job["kind"], status=status)
        return status

    def _retry(self, job: dict, worker_id: str, detail: str) -> None:
        delay = min(60, 2 ** job["attempts"])
        database = self.services.database
        with database.connection() as conn:
            Workspace(Storage(conn, dialect=database.dialect)).release_for_retry(
                job["id"], worker_id, delay_seconds=delay, error_internal=detail
            )
        log_event(logger, "job_retry", job_id=job["id"], kind=job["kind"], attempt=job["attempts"], delay=delay)

    # --- threads ---------------------------------------------------------------------

    def housekeeping(self) -> dict:
        database = self.services.database
        settings = self.services.settings
        with database.connection() as conn:
            ws = Workspace(Storage(conn, dialect=database.dialect))
            out = ws.recover_expired_jobs()
            out["pruned_jobs"] = ws.prune_finished_jobs(settings.job_retention_days)
            removed = 0
            for artifact in ws.expired_artifacts():
                try:
                    self.services.artifacts.delete(artifact["storage_key"])
                except Exception as exc:
                    logger.warning("artifact_delete_failed", exc_info=exc)
                    continue
                ws.delete_artifact_row(artifact["id"])
                removed += 1
            out["expired_artifacts"] = removed
        return out

    def _loop(self, index: int) -> None:
        worker_id = f"{self.worker_id}-{index}"
        last_housekeeping = 0.0
        idle = 0.2
        while not self._stop.is_set():
            try:
                if index == 0 and time.monotonic() - last_housekeeping > 30:
                    last_housekeeping = time.monotonic()
                    self.housekeeping()
                job = self.run_once(worker_id)
            except Exception as exc:
                logger.error("job_worker_error", exc_info=exc)
                job = None
            if job is None:
                self._stop.wait(idle)
                idle = min(2.0, idle * 1.5)
            else:
                idle = 0.2

    def start(self, workers: int) -> None:
        for index in range(workers):
            thread = threading.Thread(target=self._loop, args=(index,), name=f"arranger-job-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
