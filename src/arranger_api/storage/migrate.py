"""Apply database migrations as a release step.

    python -m arranger_api.storage.migrate            # apply pending migrations
    python -m arranger_api.storage.migrate --check    # exit 1 if any are pending

Uses the same configuration as the API (`DATABASE_URL` / `ARRANGER_DATABASE_URL`
for Postgres, otherwise `ARRANGER_DB_PATH` for SQLite). Pair it with
`RUN_MIGRATIONS_ON_STARTUP=false` when the deploy runs migrations separately
from the web process. Safe to run concurrently: the runner takes a database
lock (see `migrations.py`).
"""

from __future__ import annotations

import argparse
import sys

from .database import Database
from .migrations import migration_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arranger_api.storage.migrate",
        description="Apply Arranger API database migrations.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report pending migrations and exit 1 if there are any; change nothing.",
    )
    args = parser.parse_args(argv)

    database = Database.from_env()
    try:
        if args.check:
            with database.connection() as conn:
                status = migration_status(conn)
            print(f"dialect: {database.dialect}")
            print(f"current: {status['current'] or 'none'}")
            for migration_id in status["pending"]:
                print(f"pending: {migration_id}")
            return 1 if status["pending"] else 0

        applied = database.migrate()
        with database.connection() as conn:
            status = migration_status(conn)
        print(f"dialect: {database.dialect}")
        for migration_id in applied:
            print(f"applied: {migration_id}")
        if not applied:
            print("nothing to apply")
        print(f"current: {status['current'] or 'none'}")
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    sys.exit(main())
