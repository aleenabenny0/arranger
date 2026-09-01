"""Database connection and schema setup."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .migrations import run_migrations


POSTGRES_SCHEMES = {"postgres", "postgresql"}


try:  # pragma: no cover - exercised only when psycopg is installed.
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - local SQLite tests do not require Postgres.
    psycopg = None
    dict_row = None


INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if psycopg is not None:  # pragma: no cover - depends on optional Postgres driver.
    INTEGRITY_ERRORS = (*INTEGRITY_ERRORS, psycopg.IntegrityError)


@dataclass
class PostgresConnection:
    """Tiny DB-API adapter that lets repositories use portable placeholders."""

    raw: Any
    dialect: str = "postgres"

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> Any:
        return self.raw.execute(sql.replace("?", "%s"), params)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


def database_url() -> str | None:
    """Return the configured database URL, if one was provided."""
    return os.environ.get("ARRANGER_DATABASE_URL") or os.environ.get("DATABASE_URL")


def sqlite_database_path() -> Path:
    """Return the configured SQLite path.

    Tests can set `ARRANGER_DB_PATH`; normal local runs use `data/arranger.db`.
    """
    return Path(os.environ.get("ARRANGER_DB_PATH", "data/arranger.db"))


def connect(path: str | Path | None = None) -> sqlite3.Connection | PostgresConnection:
    """Open the configured database.

    Production/cloud deployments should set `DATABASE_URL` or
    `ARRANGER_DATABASE_URL` to a Postgres URL. Local development and tests can
    keep using SQLite through `ARRANGER_DB_PATH` or an explicit `path`.
    """
    if path is None:
        url = database_url()
        if url and urlparse(url).scheme in POSTGRES_SCHEMES:
            if psycopg is None:
                raise RuntimeError(
                    "Postgres storage requires the 'psycopg' package. "
                    "Install the API extras with: pip install -e .[api]"
                )
            raw = psycopg.connect(url, row_factory=dict_row)
            return PostgresConnection(raw)

    db_path = Path(path) if path is not None else sqlite_database_path()
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection | PostgresConnection) -> None:
    run_migrations(conn)


def init_sqlite(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scores (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT NOT NULL,
            note_count INTEGER NOT NULL,
            bar_count INTEGER NOT NULL,
            tempo_bpm REAL NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            score_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(score_id) REFERENCES scores(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS arrangements (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            score_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            status TEXT NOT NULL,
            playable INTEGER NOT NULL,
            n_hard INTEGER NOT NULL,
            n_strain INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            arranged_score_json TEXT NOT NULL,
            fidelity_json TEXT NOT NULL,
            verdict_json TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(score_id) REFERENCES scores(id) ON DELETE CASCADE,
            FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE,
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            score_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            status TEXT NOT NULL,
            accepted INTEGER NOT NULL,
            best_cost REAL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(score_id) REFERENCES scores(id) ON DELETE CASCADE,
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        );

        """
    )
    for table in ("profiles", "scores", "plans", "arrangements", "runs"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "user_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
    conn.commit()


def init_postgres(conn: PostgresConnection) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scores (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            note_count INTEGER NOT NULL,
            bar_count INTEGER NOT NULL,
            tempo_bpm DOUBLE PRECISION NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            score_id TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS arrangements (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            score_id TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
            plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            playable INTEGER NOT NULL,
            n_hard INTEGER NOT NULL,
            n_strain INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            arranged_score_json TEXT NOT NULL,
            fidelity_json TEXT NOT NULL,
            verdict_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            score_id TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            accepted INTEGER NOT NULL,
            best_cost DOUBLE PRECISION,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_profiles_user_created ON profiles(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_scores_user_created ON scores(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_plans_user_score_created ON plans(user_id, score_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_arrangements_user_created ON arrangements(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_runs_user_created ON runs(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)",
    ]
    for statement in statements:
        conn.execute(statement)
    conn.commit()
