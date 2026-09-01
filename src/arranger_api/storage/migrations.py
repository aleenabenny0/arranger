"""Small ordered database migration runner.

The project uses a lightweight repository over raw SQL, so migrations live next
to storage instead of introducing an ORM. Each migration must be idempotent
enough to tolerate a database that was previously initialized by older code.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Migration:
    id: str
    sqlite: Callable[[Any], None]
    postgres: Callable[[Any], None]


def run_migrations(conn: Any) -> None:
    dialect = "postgres" if getattr(conn, "dialect", None) == "postgres" else "sqlite"
    create_migration_table(conn, dialect)
    applied = applied_migrations(conn)
    for migration in MIGRATIONS:
        if migration.id in applied:
            continue
        if dialect == "postgres":
            migration.postgres(conn)
        else:
            migration.sqlite(conn)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration.id, utc_now_sql(conn, dialect)),
        )
        conn.commit()


def create_migration_table(conn: Any, dialect: str) -> None:
    if dialect == "postgres":
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    conn.commit()


def applied_migrations(conn: Any) -> set[str]:
    return {row["id"] for row in conn.execute("SELECT id FROM schema_migrations")}


def utc_now_sql(conn: Any, dialect: str) -> str:
    row = conn.execute(
        "SELECT CURRENT_TIMESTAMP AS now_value"
        if dialect == "postgres"
        else "SELECT datetime('now') AS now_value"
    ).fetchone()
    return row["now_value"]


def execute_many(conn: Any, statements: Iterable[str]) -> None:
    for statement in statements:
        conn.execute(statement)


def sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def sqlite_add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in sqlite_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def postgres_add_column(conn: Any, table: str, ddl: str) -> None:
    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {ddl}")


def m0001_sqlite(conn: sqlite3.Connection) -> None:
    execute_many(
        conn,
        [
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
                user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                csrf_token_hash TEXT,
                ip_address TEXT,
                user_agent TEXT,
                last_seen_at TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            """
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
            )
            """,
            """
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
            )
            """,
            """
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
            )
            """,
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_profiles_user_created ON profiles(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_scores_user_created ON scores(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_plans_user_score_created ON plans(user_id, score_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_arrangements_user_created ON arrangements(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_user_created ON runs(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)",
            "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash)",
        ],
    )


def m0001_postgres(conn: Any) -> None:
    execute_many(
        conn,
        [
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
                csrf_token_hash TEXT,
                ip_address TEXT,
                user_agent TEXT,
                last_seen_at TEXT,
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
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_profiles_user_created ON profiles(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_scores_user_created ON scores(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_plans_user_score_created ON plans(user_id, score_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_arrangements_user_created ON arrangements(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_user_created ON runs(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)",
            "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash)",
        ],
    )


def m0002_sqlite(conn: sqlite3.Connection) -> None:
    for table in ("profiles", "scores", "plans", "arrangements", "runs"):
        sqlite_add_column(conn, table, "user_id", "user_id TEXT")
    sqlite_add_column(conn, "sessions", "csrf_token_hash", "csrf_token_hash TEXT")
    sqlite_add_column(conn, "sessions", "ip_address", "ip_address TEXT")
    sqlite_add_column(conn, "sessions", "user_agent", "user_agent TEXT")
    sqlite_add_column(conn, "sessions", "last_seen_at", "last_seen_at TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash)")


def m0002_postgres(conn: Any) -> None:
    for table in ("profiles", "scores", "plans", "arrangements", "runs"):
        postgres_add_column(conn, table, "user_id TEXT")
    postgres_add_column(conn, "sessions", "csrf_token_hash TEXT")
    postgres_add_column(conn, "sessions", "ip_address TEXT")
    postgres_add_column(conn, "sessions", "user_agent TEXT")
    postgres_add_column(conn, "sessions", "last_seen_at TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash)")


MIGRATIONS = [
    Migration("0001_initial_storage", m0001_sqlite, m0001_postgres),
    Migration("0002_auth_hardening", m0002_sqlite, m0002_postgres),
]
