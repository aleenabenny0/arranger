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
from .settings import load_settings, validate_settings
from .storage import Database

logger = get_logger("arranger_api.worker")


def build_runner() -> JobRunner:
    settings = load_settings()
    validate_settings(settings)
    configure_logging(settings.log_level)
    database = Database.from_settings(settings)
    services = JobServices(
        settings=settings, database=database, artifacts=build_artifact_store(settings, database),
        metrics=AppMetrics(), engraver=LilyPondEngraver(timeout=float(settings.engrave_max_seconds)),
    )
    return JobRunner(services)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arranger-worker")
    parser.add_argument("--once", action="store_true", help="run queued jobs until the queue is empty, then exit")
    parser.add_argument("--workers", type=int, default=None, help="threads in this process (default: JOB_WORKERS or 1)")
    args = parser.parse_args(argv)

    runner = build_runner()
    runner.housekeeping()
    if args.once:
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
    runner.start(workers)
    log_event(logger, "worker_started", workers=workers)
    stop.wait()
    runner.stop()
    log_event(logger, "worker_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
