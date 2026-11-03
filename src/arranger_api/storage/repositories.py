"""Repository helpers for persisted API resources."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .migrations import migration_status

SQLITE_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


def format_timestamp(value: datetime) -> str:
    """ISO-8601 UTC with fixed microsecond precision, so strings sort like times."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def utc_now() -> str:
    return format_timestamp(datetime.now(timezone.utc))


def new_id() -> str:
    return str(uuid.uuid4())


def decode_row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for key in (
        "payload_json",
        "arranged_score_json",
        "fidelity_json",
        "verdict_json",
        "edited_plan_json",
    ):
        if key in data:
            out_key = key.removesuffix("_json")
            raw = data.pop(key)
            # Nullable JSON columns (feedback without an edited plan) stay None.
            data[out_key] = json.loads(raw) if raw is not None else None
    for key in ("playable", "accepted"):
        if key in data:
            data[key] = bool(data[key])
    return data


class Storage:
    """Small repository facade over the configured database connection.

    Every write method commits on its own unless it runs inside
    `with storage.transaction():`, in which case the whole block commits once
    on success and rolls back on any exception.
    """

    def __init__(self, conn: Any, *, dialect: str | None = None):
        self.conn = conn
        self.dialect = dialect or (
            "postgres" if getattr(conn, "dialect", None) == "postgres" else "sqlite"
        )
        self._tx_depth = 0
        self._tx_failed = False

    # --- transactions ----------------------------------------------------

    @property
    def supports_returning(self) -> bool:
        return self.dialect == "postgres" or SQLITE_HAS_RETURNING

    def _commit(self) -> None:
        """Commit now, unless an enclosing `transaction()` owns the commit."""
        if self._tx_depth == 0:
            self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    @contextmanager
    def transaction(self) -> Iterator["Storage"]:
        """Run several repository calls atomically.

        Commits when the block exits normally and rolls back when it raises.
        Nested blocks join the outermost transaction; if an inner block fails,
        the outermost one rolls back even when the caller swallowed the error.

        On SQLite the transaction starts with `BEGIN IMMEDIATE`, taking the
        write lock up front. A deferred transaction that reads and then writes
        can fail with SQLITE_BUSY when another writer got in between; taking
        the lock first makes writers queue on `busy_timeout` instead.
        """
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield self
            except BaseException:
                self._tx_failed = True
                raise
            finally:
                self._tx_depth -= 1
            return

        if self.dialect == "sqlite":
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        self._tx_failed = False
        try:
            yield self
        except BaseException:
            self._tx_depth = 0
            self.conn.rollback()
            raise
        else:
            self._tx_depth = 0
            if self._tx_failed:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            self._tx_depth = 0
            self._tx_failed = False

    # --- health ----------------------------------------------------------

    def ping(self) -> bool:
        self.conn.execute("SELECT 1").fetchone()
        self.conn.execute(
            """
            SELECT id, email, password_hash, display_name, created_at, updated_at,
                   email_verified_at
            FROM users
            LIMIT 0
            """
        )
        self.conn.execute(
            """
            SELECT id, user_id, token_hash, csrf_token_hash, ip_address, user_agent,
                   last_seen_at, created_at, expires_at, revoked_at
            FROM sessions
            LIMIT 0
            """
        )
        self.conn.execute(
            """
            SELECT id, user_id, token_hash, created_at, expires_at, used_at
            FROM password_reset_tokens
            LIMIT 0
            """
        )
        self.conn.execute(
            """
            SELECT id, user_id, token_hash, created_at, expires_at, used_at
            FROM email_verification_tokens
            LIMIT 0
            """
        )
        self.conn.execute("SELECT key, window_start, count FROM rate_limit_buckets LIMIT 0")
        self.conn.execute(
            """
            SELECT id, user_id, run_id, score_id, profile_id, region_index,
                   start_bar, end_bar, pattern, voices, melody_fold_window,
                   difficulty_score, difficulty_rank, energy, role, verifier_cost,
                   planner_penalty, chosen, created_at, payload_json
            FROM candidate_rankings
            LIMIT 0
            """
        )
        self.conn.execute(
            """
            SELECT id, user_id, candidate_ranking_id, arrangement_id, label,
                   edited_plan_json, notes, created_at, payload_json
            FROM candidate_feedback
            LIMIT 0
            """
        )
        return True

    def migration_status(self) -> dict[str, Any]:
        return migration_status(self.conn)

    # --- users -----------------------------------------------------------

    def create_user(self, email: str, password_hash: str, display_name: str) -> dict:
        now = utc_now()
        record_id = new_id()
        self.conn.execute(
            """
            INSERT INTO users
                (id, email, password_hash, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (record_id, email.lower(), password_hash, display_name, now, now),
        )
        self._commit()
        return self.get_user(record_id)

    def get_user(self, record_id: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT id, email, display_name, created_at, updated_at, email_verified_at
            FROM users WHERE id = ?
            """,
            (record_id,),
        ).fetchone()
        return decode_row(row)

    def get_user_with_password(self, email: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower(),)
        ).fetchone()
        return decode_row(row)

    def get_user_with_password_by_id(self, user_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return decode_row(row)

    def update_password_hash(self, user_id: str, password_hash: str) -> bool:
        cur = self.conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, utc_now(), user_id),
        )
        self._commit()
        return cur.rowcount > 0

    def mark_email_verified(self, user_id: str) -> bool:
        now = utc_now()
        cur = self.conn.execute(
            """
            UPDATE users SET email_verified_at = ?, updated_at = ?
            WHERE id = ? AND email_verified_at IS NULL
            """,
            (now, now, user_id),
        )
        self._commit()
        return cur.rowcount > 0

    # --- sessions --------------------------------------------------------

    def create_session(
        self,
        user_id: str,
        token_hash: str,
        days: int = 30,
        *,
        csrf_token_hash: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        max_sessions: int | None = None,
    ) -> dict:
        now_dt = datetime.now(timezone.utc)
        record_id = new_id()
        with self.transaction():
            self.prune_expired_sessions(user_id)
            if max_sessions:
                self.trim_user_sessions(user_id, keep=max_sessions - 1)
            self.conn.execute(
                """
                INSERT INTO sessions
                    (
                        id, user_id, token_hash, csrf_token_hash, ip_address,
                        user_agent, last_seen_at, created_at, expires_at, revoked_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    record_id,
                    user_id,
                    token_hash,
                    csrf_token_hash,
                    ip_address,
                    (user_agent or "")[:400],
                    format_timestamp(now_dt),
                    format_timestamp(now_dt),
                    format_timestamp(now_dt + timedelta(days=days)),
                ),
            )
        return self.get_session(record_id)

    def get_session(self, record_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (record_id,)).fetchone()
        return decode_row(row)

    def active_session(
        self,
        token_hash: str,
        *,
        idle_seconds: int | None = None,
        touch_seconds: int = 300,
    ) -> dict | None:
        """The live session for a token, joined with its user, or None.

        Live means not revoked, not past `expires_at`, and - when `idle_seconds`
        is given - seen within that many seconds. `last_seen_at` is refreshed at
        most once per `touch_seconds`, so ordinary requests are read-only.
        """
        now_dt = datetime.now(timezone.utc)
        now = format_timestamp(now_dt)
        row = self.conn.execute(
            """
            SELECT sessions.id AS session_id,
                   sessions.csrf_token_hash AS csrf_token_hash,
                   sessions.last_seen_at AS last_seen_at,
                   sessions.created_at AS session_created_at,
                   sessions.expires_at AS session_expires_at,
                   users.id AS id,
                   users.email AS email,
                   users.display_name AS display_name,
                   users.created_at AS created_at,
                   users.updated_at AS updated_at,
                   users.email_verified_at AS email_verified_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
              AND sessions.revoked_at IS NULL
              AND sessions.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        if row is None:
            return None
        data = decode_row(row)
        last_seen = data.get("last_seen_at") or data.get("session_created_at") or ""
        if idle_seconds is not None and idle_seconds > 0:
            idle_cutoff = format_timestamp(now_dt - timedelta(seconds=idle_seconds))
            if last_seen <= idle_cutoff:
                return None
        touch_cutoff = format_timestamp(now_dt - timedelta(seconds=max(touch_seconds, 0)))
        if last_seen <= touch_cutoff:
            self.conn.execute(
                """
                UPDATE sessions SET last_seen_at = ?
                WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at <= ?)
                """,
                (now, data["session_id"], touch_cutoff),
            )
            self._commit()
            data["last_seen_at"] = now
        return data

    def user_for_session(self, token_hash: str) -> dict | None:
        """Backwards-compatible lookup: the user behind a live session token."""
        data = self.active_session(token_hash)
        if data is None:
            return None
        return {
            key: data[key]
            for key in ("id", "email", "display_name", "created_at", "updated_at", "email_verified_at")
        }

    def session_for_token(self, token_hash: str) -> dict | None:
        now = utc_now()
        row = self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE token_hash = ?
              AND revoked_at IS NULL
              AND expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        return decode_row(row)

    def list_sessions(self, user_id: str, *, idle_seconds: int | None = None) -> list[dict]:
        now_dt = datetime.now(timezone.utc)
        params: list[Any] = [user_id, format_timestamp(now_dt)]
        idle_clause = ""
        if idle_seconds is not None and idle_seconds > 0:
            idle_clause = "AND COALESCE(last_seen_at, created_at) > ?"
            params.append(format_timestamp(now_dt - timedelta(seconds=idle_seconds)))
        rows = self.conn.execute(
            f"""
            SELECT id, created_at, last_seen_at, expires_at, ip_address, user_agent
            FROM sessions
            WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ? {idle_clause}
            ORDER BY created_at DESC
            """,  # nosec B608 - built only from literal fragments; every value is a bound parameter
            tuple(params),
        )
        return [decode_row(row) for row in rows]

    def revoke_session(self, token_hash: str) -> bool:
        cur = self.conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (utc_now(), token_hash),
        )
        self._commit()
        return cur.rowcount > 0

    def revoke_session_by_id(self, user_id: str, session_id: str) -> bool:
        cur = self.conn.execute(
            """
            UPDATE sessions SET revoked_at = ?
            WHERE id = ? AND user_id = ? AND revoked_at IS NULL
            """,
            (utc_now(), session_id, user_id),
        )
        self._commit()
        return cur.rowcount > 0

    def revoke_user_sessions(self, user_id: str, *, except_session_id: str | None = None) -> int:
        params: list[Any] = [utc_now(), user_id]
        keep_clause = ""
        if except_session_id is not None:
            keep_clause = "AND id <> ?"
            params.append(except_session_id)
        cur = self.conn.execute(
            f"""
            UPDATE sessions
            SET revoked_at = ?
            WHERE user_id = ? AND revoked_at IS NULL {keep_clause}
            """,  # nosec B608 - built only from literal fragments; every value is a bound parameter
            tuple(params),
        )
        self._commit()
        return cur.rowcount

    def prune_expired_sessions(self, user_id: str) -> None:
        now = utc_now()
        self.conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?
            WHERE user_id = ?
              AND revoked_at IS NULL
              AND expires_at <= ?
            """,
            (now, user_id, now),
        )
        self._commit()

    def trim_user_sessions(self, user_id: str, keep: int) -> None:
        """Revoke the oldest sessions beyond `keep`, all of them or none."""
        with self.transaction():
            rows = list(
                self.conn.execute(
                    """
                    SELECT id FROM sessions
                    WHERE user_id = ? AND revoked_at IS NULL
                    ORDER BY created_at DESC
                    """,
                    (user_id,),
                )
            )
            for row in rows[max(keep, 0):]:
                self.conn.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE id = ?",
                    (utc_now(), row["id"]),
                )

    def change_password(
        self,
        user_id: str,
        password_hash: str,
        *,
        revoke_sessions: bool = True,
    ) -> bool:
        """Set a new password hash and, atomically, revoke every session."""
        with self.transaction():
            changed = self.update_password_hash(user_id, password_hash)
            if changed and revoke_sessions:
                self.revoke_user_sessions(user_id)
        return changed

    # --- single-use tokens -----------------------------------------------

    def _create_token(self, table: str, user_id: str, token_hash: str, minutes: int) -> None:
        now_dt = datetime.now(timezone.utc)
        self.conn.execute(
            f"""
            INSERT INTO {table}
                (id, user_id, token_hash, created_at, expires_at, used_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,  # nosec B608 - table is an internal constant, never user input; values are bound parameters
            (
                new_id(),
                user_id,
                token_hash,
                format_timestamp(now_dt),
                format_timestamp(now_dt + timedelta(minutes=minutes)),
            ),
        )
        self._commit()

    def _get_token(self, table: str, token_hash: str) -> dict | None:
        row = self.conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE token_hash = ?
              AND used_at IS NULL
              AND expires_at > ?
            """,  # nosec B608 - table is an internal constant, never user input; values are bound parameters
            (token_hash, utc_now()),
        ).fetchone()
        return decode_row(row)

    def _claim_token(self, table: str, token_hash: str, now: str) -> str | None:
        """Atomically mark an unused, unexpired token used. Returns its user id.

        One conditional UPDATE decides the winner: the database re-evaluates
        `used_at IS NULL` under the row/write lock, so of N concurrent callers
        exactly one sees a changed row. There is no read-then-write window.
        """
        sql = f"""
            UPDATE {table}
            SET used_at = ?
            WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
        """  # nosec B608 - table is an internal constant, never user input; values are bound parameters
        if self.supports_returning:
            rows = self.conn.execute(sql + " RETURNING user_id", (now, token_hash, now)).fetchall()
            return rows[0]["user_id"] if rows else None
        cur = self.conn.execute(sql, (now, token_hash, now))
        if cur.rowcount != 1:
            return None
        row = self.conn.execute(
            f"SELECT user_id FROM {table} WHERE token_hash = ?", (token_hash,)  # nosec B608 - table is an internal constant, never user input; values are bound parameters
        ).fetchone()
        return row["user_id"] if row else None

    def create_password_reset_token(
        self,
        user_id: str,
        token_hash: str,
        minutes: int,
    ) -> dict:
        self._create_token("password_reset_tokens", user_id, token_hash, minutes)
        return self.get_password_reset_token(token_hash)

    def get_password_reset_token(self, token_hash: str) -> dict | None:
        return self._get_token("password_reset_tokens", token_hash)

    def consume_password_reset_token(self, token_hash: str, password_hash: str) -> dict | None:
        """Spend a reset token: new password, all sessions revoked, one transaction.

        Returns the user, or None when the token is unknown, expired or already
        used - including when another request spent it a moment ago.
        """
        with self.transaction():
            now = utc_now()
            user_id = self._claim_token("password_reset_tokens", token_hash, now)
            if user_id is None:
                return None
            # Receiving the reset email proves control of the mailbox.
            self.conn.execute(
                """
                UPDATE users
                SET password_hash = ?, updated_at = ?,
                    email_verified_at = COALESCE(email_verified_at, ?)
                WHERE id = ?
                """,
                (password_hash, now, now, user_id),
            )
            self.conn.execute(
                """
                UPDATE password_reset_tokens SET used_at = ?
                WHERE user_id = ? AND used_at IS NULL
                """,
                (now, user_id),
            )
            self.conn.execute(
                """
                UPDATE sessions
                SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (now, user_id),
            )
        return self.get_user(user_id)

    def create_email_verification_token(
        self,
        user_id: str,
        token_hash: str,
        minutes: int,
    ) -> dict:
        self._create_token("email_verification_tokens", user_id, token_hash, minutes)
        return self.get_email_verification_token(token_hash)

    def get_email_verification_token(self, token_hash: str) -> dict | None:
        return self._get_token("email_verification_tokens", token_hash)

    def consume_email_verification_token(self, token_hash: str) -> dict | None:
        """Spend a verification token and mark the user's email verified."""
        with self.transaction():
            now = utc_now()
            user_id = self._claim_token("email_verification_tokens", token_hash, now)
            if user_id is None:
                return None
            self.conn.execute(
                """
                UPDATE users
                SET email_verified_at = COALESCE(email_verified_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, user_id),
            )
            self.conn.execute(
                """
                UPDATE email_verification_tokens SET used_at = ?
                WHERE user_id = ? AND used_at IS NULL
                """,
                (now, user_id),
            )
        return self.get_user(user_id)

    # --- housekeeping ----------------------------------------------------

    def cleanup_expired(self, *, idle_seconds: int | None = None) -> dict[str, int]:
        """Delete rows that can never be used again. Safe to run from any instance."""
        now_dt = datetime.now(timezone.utc)
        now = format_timestamp(now_dt)
        removed: dict[str, int] = {}
        with self.transaction():
            session_sql = "DELETE FROM sessions WHERE revoked_at IS NOT NULL OR expires_at <= ?"
            params: list[Any] = [now]
            if idle_seconds is not None and idle_seconds > 0:
                session_sql += " OR COALESCE(last_seen_at, created_at) <= ?"
                params.append(format_timestamp(now_dt - timedelta(seconds=idle_seconds)))
            removed["sessions"] = self.conn.execute(session_sql, tuple(params)).rowcount
            for table in ("password_reset_tokens", "email_verification_tokens"):
                removed[table] = self.conn.execute(
                    f"DELETE FROM {table} WHERE used_at IS NOT NULL OR expires_at <= ?",  # nosec B608 - table is an internal constant, never user input; values are bound parameters
                    (now,),
                ).rowcount
        return {key: max(0, value or 0) for key, value in removed.items()}

    # --- profiles -------------------------------------------------------

    def create_profile(self, user_id: str, profile: dict) -> dict:
        now = utc_now()
        record_id = new_id()
        self.conn.execute(
            """
            INSERT INTO profiles (id, user_id, name, created_at, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (record_id, user_id, profile.get("name", "default"), now, now, json.dumps(profile)),
        )
        self._commit()
        return self.get_profile(user_id, record_id)

    def list_profiles(self, user_id: str, limit: int = 25, offset: int = 0) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                """
                SELECT * FROM profiles
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            )
        ]

    def get_profile(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM profiles WHERE id = ? AND user_id = ?", (record_id, user_id)
        ).fetchone()
        return decode_row(row)

    def update_profile(self, user_id: str, record_id: str, profile: dict) -> dict | None:
        now = utc_now()
        cur = self.conn.execute(
            """
            UPDATE profiles
            SET name = ?, updated_at = ?, payload_json = ?
            WHERE id = ? AND user_id = ?
            """,
            (profile.get("name", "default"), now, json.dumps(profile), record_id, user_id),
        )
        self._commit()
        return self.get_profile(user_id, record_id) if cur.rowcount else None

    def delete_profile(self, user_id: str, record_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM profiles WHERE id = ? AND user_id = ?", (record_id, user_id)
        )
        self._commit()
        return cur.rowcount > 0

    def create_score(self, user_id: str, score: dict) -> dict:
        now = utc_now()
        record_id = new_id()
        notes = score.get("notes", [])
        bars = [n.get("bar") for n in notes if n.get("bar")]
        self.conn.execute(
            """
            INSERT INTO scores
                (id, user_id, title, note_count, bar_count, tempo_bpm, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                user_id,
                score.get("title", "untitled"),
                len(notes),
                max(bars, default=1),
                score.get("tempo_bpm", 100.0),
                now,
                json.dumps(score),
            ),
        )
        self._commit()
        return self.get_score(user_id, record_id)

    def list_scores(self, user_id: str, limit: int = 25, offset: int = 0) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                """
                SELECT * FROM scores
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            )
        ]

    def get_score(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM scores WHERE id = ? AND user_id = ?", (record_id, user_id)
        ).fetchone()
        return decode_row(row)

    def delete_score(self, user_id: str, record_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM scores WHERE id = ? AND user_id = ?", (record_id, user_id)
        )
        self._commit()
        return cur.rowcount > 0

    def create_plan(self, user_id: str, score_id: str, plan: dict) -> dict:
        now = utc_now()
        record_id = new_id()
        self.conn.execute(
            """
            INSERT INTO plans (id, user_id, score_id, title, created_at, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                user_id,
                score_id,
                plan.get("title", "untitled"),
                now,
                now,
                json.dumps(plan),
            ),
        )
        self._commit()
        return self.get_plan(user_id, record_id)

    def list_plans(
        self,
        user_id: str,
        score_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict]:
        if score_id:
            rows = self.conn.execute(
                """
                SELECT * FROM plans
                WHERE user_id = ? AND score_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, score_id, limit, offset),
            )
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM plans
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            )
        return [decode_row(row) for row in rows]

    def get_plan(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM plans WHERE id = ? AND user_id = ?", (record_id, user_id)
        ).fetchone()
        return decode_row(row)

    def update_plan(self, user_id: str, record_id: str, plan: dict) -> dict | None:
        now = utc_now()
        cur = self.conn.execute(
            """
            UPDATE plans
            SET title = ?, updated_at = ?, payload_json = ?
            WHERE id = ? AND user_id = ?
            """,
            (plan.get("title", "untitled"), now, json.dumps(plan), record_id, user_id),
        )
        self._commit()
        return self.get_plan(user_id, record_id) if cur.rowcount else None

    def delete_plan(self, user_id: str, record_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM plans WHERE id = ? AND user_id = ?", (record_id, user_id)
        )
        self._commit()
        return cur.rowcount > 0

    def create_arrangement(
        self,
        *,
        user_id: str,
        score_id: str,
        plan_id: str,
        profile_id: str,
        arranged: dict,
        verdict: dict,
        fidelity: dict,
    ) -> dict:
        now = utc_now()
        record_id = new_id()
        self.conn.execute(
            """
            INSERT INTO arrangements (
                id, user_id, score_id, plan_id, profile_id, status, playable, n_hard,
                n_strain, created_at, updated_at, arranged_score_json,
                fidelity_json, verdict_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                user_id,
                score_id,
                plan_id,
                profile_id,
                "complete",
                int(bool(verdict.get("playable"))),
                int(verdict.get("n_hard", 0)),
                int(verdict.get("n_strain", 0)),
                now,
                now,
                json.dumps(arranged),
                json.dumps(fidelity),
                json.dumps(verdict),
            ),
        )
        self._commit()
        return self.get_arrangement(user_id, record_id)

    def list_arrangements(self, user_id: str, limit: int = 25, offset: int = 0) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                """
                SELECT * FROM arrangements
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            )
        ]

    def get_arrangement(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM arrangements WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        ).fetchone()
        return decode_row(row)

    def iter_arrangements(self, user_id: str, *, batch_size: int = 100) -> Iterator[dict]:
        """Every arrangement of one user, oldest first, `batch_size` rows at a time.

        On Postgres the rows stay on the server behind a named cursor and come
        over in `fetchmany` batches, so an account with thousands of saved
        arrangements is streamed rather than loaded. SQLite has no server to
        hold a cursor open; it iterates the statement in the same batches.
        """
        sql = "SELECT * FROM arrangements WHERE user_id = ? ORDER BY created_at, id"
        size = max(1, int(batch_size))
        if self.dialect == "postgres":
            with self.conn.cursor(name=f"arrangements_{uuid.uuid4().hex}", itersize=size) as cursor:
                cursor.execute(sql, (user_id,))
                while rows := cursor.fetchmany(size):
                    for row in rows:
                        yield decode_row(row)
            return
        cursor = self.conn.execute(sql, (user_id,))
        while rows := cursor.fetchmany(size):
            for row in rows:
                yield decode_row(row)

    def create_run(
        self,
        *,
        user_id: str,
        score_id: str,
        profile_id: str,
        result: dict[str, Any],
    ) -> dict:
        now = utc_now()
        record_id = new_id()
        self.conn.execute(
            """
            INSERT INTO runs
                (id, user_id, score_id, profile_id, status, accepted, best_cost, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                user_id,
                score_id,
                profile_id,
                "accepted" if result.get("accepted") else "escalated",
                int(bool(result.get("accepted"))),
                result.get("best_cost"),
                now,
                json.dumps(result),
            ),
        )
        self._commit()
        return self.get_run(user_id, record_id)

    def list_runs(self, user_id: str, limit: int = 25, offset: int = 0) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                """
                SELECT * FROM runs
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            )
        ]

    def get_run(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?", (record_id, user_id)
        ).fetchone()
        return decode_row(row)

    def create_candidate_rankings(
        self,
        *,
        user_id: str,
        score_id: str,
        profile_id: str,
        rows: list[dict[str, Any]],
        run_id: str | None = None,
    ) -> list[dict]:
        now = utc_now()
        record_ids: list[str] = []
        # One ranking set is one fact about one run: all of its rows or none.
        with self.transaction():
            for row in rows:
                record_id = new_id()
                record_ids.append(record_id)
                self.conn.execute(
                    """
                    INSERT INTO candidate_rankings (
                        id, user_id, run_id, score_id, profile_id, region_index,
                        start_bar, end_bar, pattern, voices, melody_fold_window,
                        difficulty_score, difficulty_rank, energy, role, verifier_cost,
                        planner_penalty, chosen, created_at, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        user_id,
                        run_id,
                        score_id,
                        profile_id,
                        int(row["region_index"]),
                        int(row["start_bar"]),
                        int(row["end_bar"]),
                        row["pattern"],
                        int(row["voices"]),
                        int(row["melody_fold_window"]),
                        float(row["difficulty_score"]),
                        row["difficulty_rank"],
                        row["energy"],
                        row["role"],
                        float(row["verifier_cost"]),
                        float(row["planner_penalty"]),
                        int(bool(row["chosen"])),
                        now,
                        json.dumps(row),
                    ),
                )
        return [
            record
            for record_id in record_ids
            if (record := self.get_candidate_ranking(user_id, record_id)) is not None
        ]

    def list_candidate_rankings(
        self,
        user_id: str,
        *,
        run_id: str | None = None,
        score_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        filters = ["user_id = ?"]
        params: list[Any] = [user_id]
        if run_id is not None:
            filters.append("run_id = ?")
            params.append(run_id)
        if score_id is not None:
            filters.append("score_id = ?")
            params.append(score_id)
        params.extend([limit, offset])
        rows = self.conn.execute(
            f"""
            SELECT * FROM candidate_rankings
            WHERE {' AND '.join(filters)}
            ORDER BY created_at DESC, region_index ASC, verifier_cost ASC
            LIMIT ? OFFSET ?
            """,  # nosec B608 - built only from literal fragments; every value is a bound parameter
            tuple(params),
        )
        return [decode_row(row) for row in rows]

    def get_candidate_ranking(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM candidate_rankings WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        ).fetchone()
        return decode_row(row)

    def create_candidate_feedback(
        self,
        *,
        user_id: str,
        label: str,
        candidate_ranking_id: str | None = None,
        arrangement_id: str | None = None,
        edited_plan: dict | None = None,
        notes: str = "",
    ) -> dict | None:
        if label not in {"accepted", "rejected", "edited"}:
            raise ValueError("label must be accepted, rejected, or edited")
        if candidate_ranking_id is None and arrangement_id is None:
            raise ValueError("candidate_ranking_id or arrangement_id is required")
        ranking = None
        arrangement = None
        if candidate_ranking_id is not None:
            ranking = self.get_candidate_ranking(user_id, candidate_ranking_id)
            if ranking is None:
                return None
        if arrangement_id is not None:
            arrangement = self.get_arrangement(user_id, arrangement_id)
            if arrangement is None:
                return None
        if (
            ranking is not None
            and arrangement is not None
            and ranking["score_id"] != arrangement["score_id"]
        ):
            raise ValueError(
                "candidate_ranking_id and arrangement_id must refer to the same score"
            )

        now = utc_now()
        record_id = new_id()
        payload = {
            "label": label,
            "candidate_ranking_id": candidate_ranking_id,
            "arrangement_id": arrangement_id,
            "edited_plan": edited_plan,
            "notes": notes,
        }
        self.conn.execute(
            """
            INSERT INTO candidate_feedback (
                id, user_id, candidate_ranking_id, arrangement_id, label,
                edited_plan_json, notes, created_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                user_id,
                candidate_ranking_id,
                arrangement_id,
                label,
                json.dumps(edited_plan) if edited_plan is not None else None,
                notes,
                now,
                json.dumps(payload),
            ),
        )
        self._commit()
        return self.get_candidate_feedback(user_id, record_id)

    def list_candidate_feedback(
        self,
        user_id: str,
        *,
        candidate_ranking_id: str | None = None,
        arrangement_id: str | None = None,
        label: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        filters = ["user_id = ?"]
        params: list[Any] = [user_id]
        if candidate_ranking_id is not None:
            filters.append("candidate_ranking_id = ?")
            params.append(candidate_ranking_id)
        if arrangement_id is not None:
            filters.append("arrangement_id = ?")
            params.append(arrangement_id)
        if label is not None:
            filters.append("label = ?")
            params.append(label)
        params.extend([limit, offset])
        rows = self.conn.execute(
            f"""
            SELECT * FROM candidate_feedback
            WHERE {' AND '.join(filters)}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,  # nosec B608 - built only from literal fragments; every value is a bound parameter
            tuple(params),
        )
        return [decode_row(row) for row in rows]

    def get_candidate_feedback(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM candidate_feedback WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        ).fetchone()
        return decode_row(row)
