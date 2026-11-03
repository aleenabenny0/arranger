"""Database-backed fixed-window rate-limit counters.

Shared by every API process, so a limit holds across workers and instances.
One statement does the whole job: insert the bucket at count 1, or increment it
if it exists. Because the increment happens inside the upsert, two processes
hitting the same key at the same time cannot both read the old count.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from ..security import RateLimitHit
from .database import Database

_UPSERT = """
    INSERT INTO rate_limit_buckets (key, window_start, count)
    VALUES (?, ?, 1)
    ON CONFLICT (key, window_start)
    DO UPDATE SET count = rate_limit_buckets.count + 1
"""
SQLITE_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


class DatabaseRateLimitStore:
    """`RateLimitStore` over the `rate_limit_buckets(key, window_start, count)` table.

    Windows are aligned to the epoch (`window_start = now - now % window`), so
    every process agrees on the bucket without coordinating. Closed windows are
    deleted at most once per `cleanup_interval` seconds, piggy-backed on a hit.
    """

    def __init__(
        self,
        database: Database,
        *,
        cleanup_interval: float = 60.0,
        retention_seconds: int = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.cleanup_interval = cleanup_interval
        self.retention_seconds = retention_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_cleanup = clock()
        self._longest_window = 0

    def hit(self, key: str, window_seconds: int) -> RateLimitHit:
        window = max(1, int(window_seconds))
        now = self._clock()
        window_start = int(now) - int(now) % window
        self._longest_window = max(self._longest_window, window)
        with self.database.connection() as conn:
            count = self._increment(conn, key, window_start)
            conn.commit()
        if now - self._last_cleanup >= self.cleanup_interval:
            self._maybe_cleanup(now)
        return RateLimitHit(count=count, reset_after=max(1, int(window_start + window - now + 0.999)))

    def _increment(self, conn: Any, key: str, window_start: int) -> int:
        returning = getattr(conn, "dialect", "sqlite") == "postgres" or SQLITE_HAS_RETURNING
        if returning:
            row = conn.execute(_UPSERT + " RETURNING count", (key, window_start)).fetchall()[0]
            return int(row["count"])
        # Older SQLite: take the write lock first so the read sees our own write
        # and nobody else's in between.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(_UPSERT, (key, window_start))
        row = conn.execute(
            "SELECT count FROM rate_limit_buckets WHERE key = ? AND window_start = ?",
            (key, window_start),
        ).fetchone()
        return int(row["count"])

    def _maybe_cleanup(self, now: float) -> None:
        with self._lock:
            if now - self._last_cleanup < self.cleanup_interval:
                return
            self._last_cleanup = now
        try:
            self.cleanup()
        except Exception:
            # Cleanup is housekeeping; the next interval retries.
            pass

    def cleanup(self) -> int:
        cutoff = int(self._clock()) - max(self.retention_seconds, 2 * self._longest_window)
        with self.database.connection() as conn:
            cur = conn.execute("DELETE FROM rate_limit_buckets WHERE window_start < ?", (cutoff,))
            removed = cur.rowcount
            conn.commit()
        return max(0, removed or 0)
