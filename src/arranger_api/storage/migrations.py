"""Small ordered database migration runner.

The project uses a lightweight repository over raw SQL, so migrations live next
to storage instead of introducing an ORM. Each migration must be idempotent
enough to tolerate a database that was previously initialized by older code.

Migrations run once, at startup (the FastAPI lifespan) or as a release step
(`python -m arranger_api.storage.migrate`) - never per request. Two instances
starting together must not both apply the same migration, so the runner
serialises itself:

- Postgres: a session-level `pg_advisory_lock` held for the whole run.
- SQLite: each migration runs inside `BEGIN IMMEDIATE`, which takes the write
  lock up front; the applied-set is re-read under that lock, so a process that
  lost the race sees the migration as applied and skips it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

# Arbitrary constant identifying "arranger schema migrations" to pg_advisory_lock.
MIGRATION_ADVISORY_LOCK_ID = 4_158_226_001


@dataclass(frozen=True)
class Migration:
    id: str
    sqlite: Callable[[Any], None]
    postgres: Callable[[Any], None]


def dialect_of(conn: Any) -> str:
    return "postgres" if getattr(conn, "dialect", None) == "postgres" else "sqlite"


@contextmanager
def _postgres_migration_lock(conn: Any) -> Iterator[None]:
    conn.execute("SELECT pg_advisory_lock(?)", (MIGRATION_ADVISORY_LOCK_ID,))
    try:
        yield
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.execute("SELECT pg_advisory_unlock(?)", (MIGRATION_ADVISORY_LOCK_ID,))
        conn.commit()


def run_migrations(conn: Any) -> list[str]:
    """Apply every pending migration. Returns the ids applied by this call."""
    if dialect_of(conn) == "postgres":
        with _postgres_migration_lock(conn):
            return _run_postgres(conn)
    return _run_sqlite(conn)


def _run_postgres(conn: Any) -> list[str]:
    create_migration_table(conn, "postgres")
    applied_now: list[str] = []
    already = applied_migrations(conn)
    for migration in MIGRATIONS:
        if migration.id in already:
            continue
        try:
            migration.postgres(conn)
            conn.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (migration.id, utc_now_sql(conn, "postgres")),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        applied_now.append(migration.id)
    return applied_now


def _run_sqlite(conn: sqlite3.Connection) -> list[str]:
    if conn.in_transaction:
        conn.commit()
    create_migration_table(conn, "sqlite")
    applied_now: list[str] = []
    for migration in MIGRATIONS:
        if migration.id in applied_migrations(conn):
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: another process may have applied
            # this migration between the read above and the lock being granted.
            if migration.id in applied_migrations(conn):
                conn.rollback()
                continue
            migration.sqlite(conn)
            conn.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (migration.id, utc_now_sql(conn, "sqlite")),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        applied_now.append(migration.id)
    return applied_now


def migration_status(conn: Any) -> dict[str, Any]:
    """Current schema version and what is still pending, without changing anything."""
    try:
        applied = applied_migrations(conn)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        applied = set()
    known = [migration.id for migration in MIGRATIONS]
    done = [mid for mid in known if mid in applied]
    return {
        "current": done[-1] if done else None,
        "applied": len(done),
        "pending": [mid for mid in known if mid not in applied],
    }


def create_migration_table(conn: Any, dialect: str) -> None:
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
            """
            CREATE TABLE IF NOT EXISTS candidate_rankings (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                run_id TEXT,
                score_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                region_index INTEGER NOT NULL,
                start_bar INTEGER NOT NULL,
                end_bar INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                voices INTEGER NOT NULL,
                melody_fold_window INTEGER NOT NULL,
                difficulty_score REAL NOT NULL,
                difficulty_rank TEXT NOT NULL,
                energy TEXT NOT NULL,
                role TEXT NOT NULL,
                verifier_cost REAL NOT NULL,
                planner_penalty REAL NOT NULL,
                chosen INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
                FOREIGN KEY(score_id) REFERENCES scores(id) ON DELETE CASCADE,
                FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_profiles_user_created ON profiles(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_scores_user_created ON scores(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_plans_user_score_created ON plans(user_id, score_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_arrangements_user_created ON arrangements(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_user_created ON runs(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_user_created ON candidate_rankings(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_run ON candidate_rankings(run_id)",
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
            """
            CREATE TABLE IF NOT EXISTS candidate_rankings (
                id TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
                score_id TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
                profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                region_index INTEGER NOT NULL,
                start_bar INTEGER NOT NULL,
                end_bar INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                voices INTEGER NOT NULL,
                melody_fold_window INTEGER NOT NULL,
                difficulty_score DOUBLE PRECISION NOT NULL,
                difficulty_rank TEXT NOT NULL,
                energy TEXT NOT NULL,
                role TEXT NOT NULL,
                verifier_cost DOUBLE PRECISION NOT NULL,
                planner_penalty DOUBLE PRECISION NOT NULL,
                chosen INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_profiles_user_created ON profiles(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_scores_user_created ON scores(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_plans_user_score_created ON plans(user_id, score_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_arrangements_user_created ON arrangements(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_user_created ON runs(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_user_created ON candidate_rankings(user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_run ON candidate_rankings(run_id)",
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


UNUSABLE_PASSWORD_HASH = "unusable_password_hash"


def m0003_sqlite(conn: sqlite3.Connection) -> None:
    sqlite_add_column(conn, "users", "password_hash", "password_hash TEXT")
    sqlite_add_column(conn, "users", "display_name", "display_name TEXT")
    sqlite_add_column(conn, "users", "created_at", "created_at TEXT")
    sqlite_add_column(conn, "users", "updated_at", "updated_at TEXT")
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE password_hash IS NULL OR password_hash = ''",
        (UNUSABLE_PASSWORD_HASH,),
    )
    conn.execute(
        """
        UPDATE users
        SET display_name = substr(email, 1, instr(email || '@', '@') - 1)
        WHERE display_name IS NULL OR display_name = ''
        """
    )
    conn.execute(
        "UPDATE users SET created_at = datetime('now') WHERE created_at IS NULL OR created_at = ''"
    )
    conn.execute(
        "UPDATE users SET updated_at = created_at WHERE updated_at IS NULL OR updated_at = ''"
    )

    sqlite_add_column(conn, "sessions", "csrf_token_hash", "csrf_token_hash TEXT")
    sqlite_add_column(conn, "sessions", "ip_address", "ip_address TEXT")
    sqlite_add_column(conn, "sessions", "user_agent", "user_agent TEXT")
    sqlite_add_column(conn, "sessions", "last_seen_at", "last_seen_at TEXT")


def m0003_postgres(conn: Any) -> None:
    postgres_add_column(conn, "users", "password_hash TEXT")
    postgres_add_column(conn, "users", "display_name TEXT")
    postgres_add_column(conn, "users", "created_at TEXT")
    postgres_add_column(conn, "users", "updated_at TEXT")
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE password_hash IS NULL OR password_hash = ''",
        (UNUSABLE_PASSWORD_HASH,),
    )
    conn.execute(
        """
        UPDATE users
        SET display_name = split_part(email, '@', 1)
        WHERE display_name IS NULL OR display_name = ''
        """
    )
    conn.execute(
        "UPDATE users SET created_at = CURRENT_TIMESTAMP::text WHERE created_at IS NULL OR created_at = ''"
    )
    conn.execute(
        "UPDATE users SET updated_at = created_at WHERE updated_at IS NULL OR updated_at = ''"
    )
    conn.execute("ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL")
    conn.execute("ALTER TABLE users ALTER COLUMN display_name SET NOT NULL")
    conn.execute("ALTER TABLE users ALTER COLUMN created_at SET NOT NULL")
    conn.execute("ALTER TABLE users ALTER COLUMN updated_at SET NOT NULL")

    postgres_add_column(conn, "sessions", "csrf_token_hash TEXT")
    postgres_add_column(conn, "sessions", "ip_address TEXT")
    postgres_add_column(conn, "sessions", "user_agent TEXT")
    postgres_add_column(conn, "sessions", "last_seen_at TEXT")


def m0004_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_rankings (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            run_id TEXT,
            score_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            region_index INTEGER NOT NULL,
            start_bar INTEGER NOT NULL,
            end_bar INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            voices INTEGER NOT NULL,
            melody_fold_window INTEGER NOT NULL,
            difficulty_score REAL NOT NULL,
            difficulty_rank TEXT NOT NULL,
            energy TEXT NOT NULL,
            role TEXT NOT NULL,
            verifier_cost REAL NOT NULL,
            planner_penalty REAL NOT NULL,
            chosen INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
            FOREIGN KEY(score_id) REFERENCES scores(id) ON DELETE CASCADE,
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_user_created ON candidate_rankings(user_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_run ON candidate_rankings(run_id)"
    )


def m0004_postgres(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_rankings (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
            score_id TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            region_index INTEGER NOT NULL,
            start_bar INTEGER NOT NULL,
            end_bar INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            voices INTEGER NOT NULL,
            melody_fold_window INTEGER NOT NULL,
            difficulty_score DOUBLE PRECISION NOT NULL,
            difficulty_rank TEXT NOT NULL,
            energy TEXT NOT NULL,
            role TEXT NOT NULL,
            verifier_cost DOUBLE PRECISION NOT NULL,
            planner_penalty DOUBLE PRECISION NOT NULL,
            chosen INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_user_created ON candidate_rankings(user_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_rankings_run ON candidate_rankings(run_id)"
    )


def m0005_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_feedback (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            candidate_ranking_id TEXT,
            arrangement_id TEXT,
            label TEXT NOT NULL CHECK (label IN ('accepted', 'rejected', 'edited')),
            edited_plan_json TEXT,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(candidate_ranking_id) REFERENCES candidate_rankings(id) ON DELETE CASCADE,
            FOREIGN KEY(arrangement_id) REFERENCES arrangements(id) ON DELETE CASCADE,
            CHECK (candidate_ranking_id IS NOT NULL OR arrangement_id IS NOT NULL)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_feedback_user_created ON candidate_feedback(user_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_feedback_candidate ON candidate_feedback(candidate_ranking_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_feedback_arrangement ON candidate_feedback(arrangement_id)"
    )


def m0005_postgres(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_feedback (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            candidate_ranking_id TEXT REFERENCES candidate_rankings(id) ON DELETE CASCADE,
            arrangement_id TEXT REFERENCES arrangements(id) ON DELETE CASCADE,
            label TEXT NOT NULL CHECK (label IN ('accepted', 'rejected', 'edited')),
            edited_plan_json TEXT,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            CHECK (candidate_ranking_id IS NOT NULL OR arrangement_id IS NOT NULL)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_feedback_user_created ON candidate_feedback(user_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_feedback_candidate ON candidate_feedback(candidate_ranking_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_feedback_arrangement ON candidate_feedback(arrangement_id)"
    )


def m0006_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            key TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (key, window_start)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_buckets_window ON rate_limit_buckets(window_start)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")


def m0006_postgres(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            key TEXT NOT NULL,
            window_start BIGINT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (key, window_start)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_buckets_window ON rate_limit_buckets(window_start)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")


def m0007_sqlite(conn: sqlite3.Connection) -> None:
    sqlite_add_column(conn, "users", "email_verified_at", "email_verified_at TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_user ON email_verification_tokens(user_id)"
    )


def m0007_postgres(conn: Any) -> None:
    postgres_add_column(conn, "users", "email_verified_at TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_user ON email_verification_tokens(user_id)"
    )


# --- 0008: projects, revisions, jobs, artifacts -------------------------------
#
# A project is one piece of music a user is working on. It owns:
#   project_sources       the imported score and every correction to it, as revisions
#   project_arrangements  every arrangement made from a source, as revisions. Each row
#                         pins the source revision, the profile, the plan, and the
#                         algorithm and model versions, so an old result stays
#                         explainable and reproducible after the engine changes.
#   artifacts             metadata for files (uploads, MIDI, MusicXML, PDF). The bytes
#                         live in an ArtifactStore under `storage_key`.
#   jobs                  background work, with lease-based claiming so any number of
#                         workers on any number of hosts can share the queue.
#
# `artifact_blobs` backs the database artifact store, for deployments with no
# object storage.

_PROJECT_TABLES = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    composer TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    current_source_id TEXT,
    current_arrangement_id TEXT,
    profile_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_user_updated ON projects(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS project_sources (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    note_count INTEGER NOT NULL DEFAULT 0,
    bar_count INTEGER NOT NULL DEFAULT 0,
    score_json TEXT NOT NULL,
    selection_json TEXT,
    inspection_json TEXT,
    upload_artifact_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_project_sources_project ON project_sources(project_id, revision DESC);

CREATE TABLE IF NOT EXISTS project_arrangements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    source_id TEXT NOT NULL REFERENCES project_sources(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    n_hard INTEGER NOT NULL DEFAULT 0,
    n_strain INTEGER NOT NULL DEFAULT 0,
    fidelity_score REAL NOT NULL DEFAULT 0,
    difficulty REAL NOT NULL DEFAULT 0,
    algorithm_version TEXT NOT NULL,
    model TEXT,
    profile_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    arranged_json TEXT NOT NULL,
    verdict_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_project_arrangements_project ON project_arrangements(project_id, revision DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    arrangement_id TEXT,
    source_id TEXT,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_arrangement ON artifacts(arrangement_id, kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_expires ON artifacts(expires_at);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error_public TEXT,
    error_internal TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    idempotency_key TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    run_after TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, run_after);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(user_id, idempotency_key);
"""


def _statements(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]


def m0008_sqlite(conn: sqlite3.Connection) -> None:
    for statement in _statements(_PROJECT_TABLES):
        conn.execute(statement)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_blobs (storage_key TEXT PRIMARY KEY, data BLOB NOT NULL)"
    )


def m0008_postgres(conn: Any) -> None:
    for statement in _statements(_PROJECT_TABLES):
        conn.execute(statement.replace(" REAL ", " DOUBLE PRECISION "))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_blobs (storage_key TEXT PRIMARY KEY, data BYTEA NOT NULL)"
    )


MIGRATIONS = [
    Migration("0001_initial_storage", m0001_sqlite, m0001_postgres),
    Migration("0002_auth_hardening", m0002_sqlite, m0002_postgres),
    Migration("0003_auth_schema_backfill", m0003_sqlite, m0003_postgres),
    Migration("0004_candidate_rankings", m0004_sqlite, m0004_postgres),
    Migration("0005_candidate_feedback", m0005_sqlite, m0005_postgres),
    Migration("0006_rate_limit_buckets", m0006_sqlite, m0006_postgres),
    Migration("0007_email_verification", m0007_sqlite, m0007_postgres),
    Migration("0008_projects_jobs_artifacts", m0008_sqlite, m0008_postgres),
]
