"""Run background jobs in their own process.

    python -m arranger_api.worker            # run until interrupted
    python -m arranger_api.worker --once     # drain the queue and exit

Use this with `JOB_WORKERS=0` on the API when arranging or transcription load
should not compete with request handling, or to scale workers independently.
Any number of worker processes on any number of hosts may share one database:
jobs are claimed under a lease, so none is ever run twice at once.
"""

from __future__ import annotations

import argparse
import signal
import threading

from arranger.adapters.lilypond import LilyPondEngraver

from .artifacts import build_artifact_store
from .jobs import JobRunner, JobServices
from .metrics import AppMetrics
from .observability import configure_logging, get_logger, log_event
from .queue import MAX_LONG_POLL_SECONDS, JobQueue, build_job_queue
from .settings import load_settings, validate_settings
from .storage import Database, Storage
from .storage.workspace import Workspace

logger = get_logger("arranger_api.worker")


def build_runner() -> JobRunner:
    settings = load_settings()
    validate_settings(settings)
    configure_logging(settings.log_level)
    database = Database.from_settings(settings)
    services = JobServices(
        settings=settings, database=database, artifacts=build_artifact_store(settings, database),
        metrics=AppMetrics(), engraver=LilyPondEngraver(timeout=float(settings.engrave_max_seconds)),
        queue=build_job_queue(settings),
    )
    return JobRunner(services)


def _set_aside(runner: JobRunner, job_id: str, reason: str) -> None:
    database = runner.services.database
    with database.connection() as conn:
        Workspace(Storage(conn, dialect=database.dialect)).set_aside_job(job_id, reason)


def consume(
    runner: JobRunner, queue: JobQueue, *, stop: threading.Event | None = None,
    worker_id: str | None = None, wait_seconds: int = MAX_LONG_POLL_SECONDS, once: bool = False,
) -> dict[str, int]:
    """Long-poll the queue and run each job it names, until `stop` is set.

    Every message is handled idempotently by job id (`JobRunner.run_job`),
    deleted only once the job row is in a final state, left for redelivery
    when the job wants another attempt, and moved to the dead-letter queue
    when it has failed `queue.max_receives` times or arrives more often than
    that. With `once`, the loop drains what is waiting and returns.
    """
    settings = runner.services.settings
    worker_id = worker_id or runner.worker_id
    counts = {"ran": 0, "skipped": 0, "retried": 0, "dead_lettered": 0}
    while stop is None or not stop.is_set():
        message = queue.receive(wait_seconds=wait_seconds, visibility_seconds=settings.effective_visibility_seconds)
        if message is None:
            if once:
                break
            continue
        if message.attempt > queue.max_receives:
            _set_aside(runner, message.job_id, f"delivered {message.attempt} times")
            queue.dead_letter(message, "delivered too many times")
            counts["dead_lettered"] += 1
            log_event(logger, "job_dead_lettered", job_id=message.job_id, attempt=message.attempt)
            continue
        outcome = runner.run_job(message.job_id, worker_id)
        log_event(logger, "queue_message_handled", job_id=message.job_id, attempt=message.attempt, outcome=outcome)
        if outcome == "retry":
            if message.attempt >= queue.max_receives:
                _set_aside(runner, message.job_id, f"failed on {message.attempt} deliveries")
                queue.dead_letter(message, f"failed on {message.attempt} deliveries")
                counts["dead_lettered"] += 1
            else:
                # Back off like the table poller does; the message returns after the delay.
                queue.retry_later(message, min(60, 2 ** message.attempt))
                counts["retried"] += 1
            continue
        queue.delete(message)
        counts["skipped" if outcome == "skipped" else "ran"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arranger-worker")
    parser.add_argument("--once", action="store_true", help="run queued jobs until the queue is empty, then exit")
    parser.add_argument("--workers", type=int, default=None, help="threads in this process (default: JOB_WORKERS or 1)")
    args = parser.parse_args(argv)

    runner = build_runner()
    runner.housekeeping()
    queue = runner.services.queue
    if args.once:
        if queue is not None:
            counts = consume(runner, queue, once=True, wait_seconds=0)
            log_event(logger, "worker_drained", queue=queue.name, **counts)
            return 0
        count = 0
        while runner.run_once() is not None:
            count += 1
        log_event(logger, "worker_drained", jobs=count)
        return 0

    workers = args.workers or max(1, runner.services.settings.job_workers)
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    if queue is not None:
        # One long-polling consumer per worker thread; each receive hands one job to this process.
        threads = [
            threading.Thread(target=consume, args=(runner, queue), kwargs={"stop": stop, "worker_id": f"{runner.worker_id}-{i}"},
                             name=f"arranger-queue-{i}", daemon=True)
            for i in range(workers)
        ]
        for thread in threads:
            thread.start()
        log_event(logger, "worker_started", workers=workers, queue=queue.name)
        stop.wait()
        for thread in threads:
            thread.join(timeout=MAX_LONG_POLL_SECONDS + 5)
        log_event(logger, "worker_stopped")
        return 0
    runner.start(workers)
    log_event(logger, "worker_started", workers=workers)
    stop.wait()
    runner.stop()
    log_event(logger, "worker_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
