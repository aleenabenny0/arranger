"""Database connections: a bounded Postgres pool, per-request SQLite, migrations.

Connection strategy
-------------------
Postgres: `psycopg_pool` is not a dependency of this project, so `ConnectionPool`
below is a deliberately small thread-safe bounded pool. A semaphore caps the
number of live connections (`DB_POOL_SIZE`), callers wait at most
`DB_POOL_TIMEOUT_SECONDS` for one, idle connections are health-checked before
reuse, every connection is retired after `DB_POOL_MAX_LIFETIME_SECONDS`, and a
connection is always rolled back before it returns to the pool so no request
inherits another's transaction. Each connection gets a server-side
`statement_timeout` so one slow query cannot hold a pool slot forever.

SQLite: connections are cheap and file-local, so each request opens its own
with `foreign_keys=ON`, `journal_mode=WAL` and a `busy_timeout`. WAL lets
readers proceed while one writer commits; `busy_timeout` makes concurrent
writers queue instead of failing with "database is locked".
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .migrations import run_migrations

POSTGRES_SCHEMES = {"postgres", "postgresql"}
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5_000


try:  # pragma: no cover - exercised only when psycopg is installed.
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - local SQLite tests do not require Postgres.
    psycopg = None
    dict_row = None


INTEGRITY_ERRORS: tuple[type[Exception], ...] = (sqlite3.IntegrityError,)
if psycopg is not None:  # pragma: no cover - depends on optional Postgres driver.
    INTEGRITY_ERRORS = (*INTEGRITY_ERRORS, psycopg.IntegrityError)


class DatabaseUnavailable(RuntimeError):
    """No usable connection: the pool is exhausted or the server is unreachable."""


class PoolTimeout(DatabaseUnavailable):
    """Every pooled connection stayed busy for the whole wait."""


@dataclass
class PostgresConnection:
    """Tiny DB-API adapter that lets repositories use portable placeholders."""

    raw: Any
    dialect: str = "postgres"

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> Any:
        return self.raw.execute(sql.replace("?", "%s"), params)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    @property
    def closed(self) -> bool:
        return bool(getattr(self.raw, "closed", False) or getattr(self.raw, "broken", False))


def database_url() -> str | None:
    """Return the configured database URL, if one was provided."""
    return os.environ.get("ARRANGER_DATABASE_URL") or os.environ.get("DATABASE_URL")


def sqlite_database_path() -> Path:
    """Return the configured SQLite path.

    Tests can set `ARRANGER_DB_PATH`; normal local runs use `data/arranger.db`.
    """
    return Path(os.environ.get("ARRANGER_DB_PATH", "data/arranger.db"))


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and urlparse(url).scheme in POSTGRES_SCHEMES


def connect_sqlite(
    path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    db_path = Path(path)
    in_memory = str(path) == ":memory:"
    if not in_memory:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        ":memory:" if in_memory else db_path,
        check_same_thread=False,
        timeout=max(busy_timeout_ms, 0) / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(max(busy_timeout_ms, 0))}")
    if not in_memory:
        try:
            _enable_wal(conn, busy_timeout_ms)
        except BaseException:
            conn.close()
            raise
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _enable_wal(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """Switch the file to write-ahead logging, waiting out other first connections.

    Changing the journal mode needs an exclusive lock, and SQLite does not call
    the busy handler for that upgrade: when a web process and a worker open a
    brand-new file together, the loser gets "database is locked" at once,
    whatever `busy_timeout` says. The mode is stored in the file, so the retry
    only ever matters on the very first start; afterwards this is a read.
    """
    deadline = time.monotonic() + max(busy_timeout_ms, 0) / 1000
    delay = 0.005
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL").fetchone()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def connect_postgres(
    url: str,
    *,
    statement_timeout_ms: int = 15_000,
    connect_timeout: int = 10,
) -> PostgresConnection:
    if psycopg is None:
        raise RuntimeError(
            "Postgres storage requires the 'psycopg' package. "
            "Install the API extras with: pip install -e .[api]"
        )
    raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=connect_timeout)
    if statement_timeout_ms > 0:
        # set_config(..., false) is session-scoped and, unlike SET, takes bind
        # parameters. Commit so a later rollback cannot undo it.
        raw.execute("SELECT set_config('statement_timeout', %s, false)", (str(statement_timeout_ms),))
        raw.execute(
            "SELECT set_config('idle_in_transaction_session_timeout', %s, false)",
            (str(statement_timeout_ms * 4),),
        )
        raw.commit()
    return PostgresConnection(raw)


def connect(path: str | Path | None = None) -> sqlite3.Connection | PostgresConnection:
    """Open one unmanaged connection to the configured database.

    Used by tests, the migration CLI and scripts. The API itself goes through
    `Database`, which adds pooling. Production/cloud deployments set
    `DATABASE_URL` or `ARRANGER_DATABASE_URL` to a Postgres URL; local
    development and tests use SQLite through `ARRANGER_DB_PATH` or `path`.
    """
    if path is None:
        url = database_url()
        if is_postgres_url(url):
            return connect_postgres(url)
    return connect_sqlite(path if path is not None else sqlite_database_path())


def init_db(conn: sqlite3.Connection | PostgresConnection) -> list[str]:
    """Apply pending migrations on `conn`. Returns the ids that were applied."""
    return run_migrations(conn)


# --- pool -------------------------------------------------------------------


@dataclass
class _PoolEntry:
    conn: Any
    created_at: float
    last_used_at: float


@dataclass
class PoolStats:
    max_size: int
    in_use: int
    idle: int
    created: int
    discarded: int


class ConnectionPool:
    """Minimal thread-safe bounded connection pool.

    `connect` opens a connection; `reset` returns it to a clean state (rollback)
    and reports whether it is reusable; `is_healthy` is asked before reusing a
    connection that sat idle longer than `health_check_after` seconds.
    """

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        max_size: int = 5,
        timeout: float = 10.0,
        max_lifetime: float = 1800.0,
        health_check_after: float = 30.0,
        is_healthy: Callable[[Any], bool] | None = None,
        reset: Callable[[Any], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_size = max(1, int(max_size))
        self.timeout = float(timeout)
        self.max_lifetime = float(max_lifetime)
        self.health_check_after = float(health_check_after)
        self._connect = connect
        self._is_healthy = is_healthy or (lambda conn: True)
        self._reset = reset or (lambda conn: True)
        self._clock = clock
        self._slots = threading.BoundedSemaphore(self.max_size)
        self._lock = threading.Lock()
        self._idle: deque[_PoolEntry] = deque()
        self._in_use: dict[int, _PoolEntry] = {}
        self._closed = False
        self._created = 0
        self._discarded = 0

    def acquire(self) -> Any:
        if self._closed:
            raise DatabaseUnavailable("connection pool is closed")
        if not self._slots.acquire(timeout=self.timeout):
            raise PoolTimeout(f"no database connection became free within {self.timeout:g}s")
        try:
            entry = self._checkout_idle()
            if entry is None:
                now = self._clock()
                entry = _PoolEntry(self._connect(), created_at=now, last_used_at=now)
                with self._lock:
                    self._created += 1
            with self._lock:
                self._in_use[id(entry.conn)] = entry
            return entry.conn
        except BaseException:
            self._slots.release()
            raise

    def _checkout_idle(self) -> _PoolEntry | None:
        while True:
            with self._lock:
                if not self._idle:
                    return None
                entry = self._idle.pop()  # LIFO keeps the working set warm
            now = self._clock()
            stale = now - entry.created_at >= self.max_lifetime
            idle_long = now - entry.last_used_at >= self.health_check_after
            if stale or getattr(entry.conn, "closed", False):
                self._discard(entry.conn)
                continue
            if idle_long and not self._safe(self._is_healthy, entry.conn):
                self._discard(entry.conn)
                continue
            return entry

    def release(self, conn: Any, *, discard: bool = False) -> None:
        with self._lock:
            entry = self._in_use.pop(id(conn), None)
        if entry is None:  # not ours, or released twice
            return
        try:
            now = self._clock()
            reusable = (
                not discard
                and not self._closed
                and not getattr(conn, "closed", False)
                and now - entry.created_at < self.max_lifetime
                and self._safe(self._reset, conn)
            )
            if reusable:
                entry.last_used_at = now
                with self._lock:
                    self._idle.append(entry)
            else:
                self._discard(conn)
        finally:
            self._slots.release()

    @staticmethod
    def _safe(fn: Callable[[Any], bool], conn: Any) -> bool:
        try:
            return bool(fn(conn))
        except Exception:
            return False

    def _discard(self, conn: Any) -> None:
        with self._lock:
            self._discarded += 1
        try:
            conn.close()
        except Exception:
            pass

    def stats(self) -> PoolStats:
        with self._lock:
            return PoolStats(
                max_size=self.max_size,
                in_use=len(self._in_use),
                idle=len(self._idle),
                created=self._created,
                discarded=self._discarded,
            )

    def close(self) -> None:
        self._closed = True
        with self._lock:
            idle = list(self._idle)
            self._idle.clear()
        for entry in idle:
            self._discard(entry.conn)


# --- database facade --------------------------------------------------------


def _postgres_reset(conn: PostgresConnection) -> bool:
    conn.rollback()
    return not conn.closed


def _postgres_healthy(conn: PostgresConnection) -> bool:
    conn.execute("SELECT 1").fetchone()
    conn.rollback()
    return True


@dataclass
class Database:
    """Hands out connections for one configured database.

    `acquire()`/`release()` (or the `connection()` context manager) are the only
    way the API touches a connection. Nothing is opened until first use, so
    constructing a `Database` never fails on an unreachable server.
    """

    url: str = ""
    sqlite_path: str | Path = "data/arranger.db"
    pool_size: int = 5
    pool_timeout: float = 10.0
    max_lifetime: float = 1800.0
    statement_timeout_ms: int = 15_000
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS
    _pool: ConnectionPool | None = field(default=None, init=False, repr=False)
    _memory_conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _memory_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)
    _init_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_settings(cls, settings: Any) -> "Database":
        return cls(
            url=settings.database_url if is_postgres_url(settings.database_url) else "",
            sqlite_path=settings.sqlite_path,
            pool_size=settings.db_pool_size,
            pool_timeout=settings.db_pool_timeout_seconds,
            max_lifetime=settings.db_pool_max_lifetime_seconds,
            statement_timeout_ms=settings.db_statement_timeout_ms,
            busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        )

    @classmethod
    def from_env(cls) -> "Database":
        url = database_url() or ""
        return cls(url=url if is_postgres_url(url) else "", sqlite_path=sqlite_database_path())

    @property
    def dialect(self) -> str:
        return "postgres" if self.url else "sqlite"

    @property
    def is_memory(self) -> bool:
        return not self.url and str(self.sqlite_path) == ":memory:"

    def _postgres_pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        lambda: connect_postgres(
                            self.url, statement_timeout_ms=self.statement_timeout_ms
                        ),
                        max_size=self.pool_size,
                        timeout=self.pool_timeout,
                        max_lifetime=self.max_lifetime,
                        is_healthy=_postgres_healthy,
                        reset=_postgres_reset,
                    )
        return self._pool

    def acquire(self) -> sqlite3.Connection | PostgresConnection:
        if self.url:
            return self._postgres_pool().acquire()
        if self.is_memory:
            # One shared connection: a second ":memory:" connection would be a
            # different, empty database. Access is serialised instead.
            if not self._memory_lock.acquire(timeout=self.pool_timeout):
                raise PoolTimeout("in-memory database stayed busy")
            if self._memory_conn is None:
                self._memory_conn = connect_sqlite(":memory:", busy_timeout_ms=self.busy_timeout_ms)
            return self._memory_conn
        return connect_sqlite(self.sqlite_path, busy_timeout_ms=self.busy_timeout_ms)

    def release(self, conn: Any, *, discard: bool = False) -> None:
        if self.url:
            self._postgres_pool().release(conn, discard=discard)
            return
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        if self.is_memory and conn is self._memory_conn:
            self._memory_lock.release()
            return
        conn.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection | PostgresConnection]:
        conn = self.acquire()
        try:
            yield conn
        finally:
            # release() rolls back whatever the caller left open and drops
            # connections that died, so success and failure look the same here.
            self.release(conn)

    def migrate(self) -> list[str]:
        with self.connection() as conn:
            return run_migrations(conn)

    def stats(self) -> PoolStats | None:
        return self._pool.stats() if self._pool is not None else None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
        if self._memory_conn is not None:
            try:
                self._memory_conn.close()
            finally:
                self._memory_conn = None
