"""Repository helpers for persisted API resources."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    ):
        if key in data:
            out_key = key.removesuffix("_json")
            data[out_key] = json.loads(data.pop(key))
    for key in ("playable", "accepted"):
        if key in data:
            data[key] = bool(data[key])
    return data


class Storage:
    """Small repository facade over the configured database connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- users and sessions --------------------------------------------

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
        self.conn.commit()
        return self.get_user(record_id)

    def get_user(self, record_id: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT id, email, display_name, created_at, updated_at
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
                user_agent,
                now_dt.isoformat(),
                now_dt.isoformat(),
                (now_dt + timedelta(days=days)).isoformat(),
            ),
        )
        self.conn.commit()
        return self.get_session(record_id)

    def get_session(self, record_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (record_id,)).fetchone()
        return decode_row(row)

    def user_for_session(self, token_hash: str) -> dict | None:
        now = utc_now()
        row = self.conn.execute(
            """
            SELECT users.id, users.email, users.display_name, users.created_at, users.updated_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
              AND sessions.revoked_at IS NULL
              AND sessions.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
            self.conn.commit()
        return decode_row(row)

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

    def revoke_session(self, token_hash: str) -> bool:
        cur = self.conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (utc_now(), token_hash),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def prune_expired_sessions(self, user_id: str) -> None:
        self.conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?
            WHERE user_id = ?
              AND revoked_at IS NULL
              AND expires_at <= ?
            """,
            (utc_now(), user_id, utc_now()),
        )
        self.conn.commit()

    def trim_user_sessions(self, user_id: str, keep: int) -> None:
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
        self.conn.commit()

    def create_password_reset_token(
        self,
        user_id: str,
        token_hash: str,
        minutes: int,
    ) -> dict:
        now_dt = datetime.now(timezone.utc)
        record_id = new_id()
        self.conn.execute(
            """
            INSERT INTO password_reset_tokens
                (id, user_id, token_hash, created_at, expires_at, used_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                record_id,
                user_id,
                token_hash,
                now_dt.isoformat(),
                (now_dt + timedelta(minutes=minutes)).isoformat(),
            ),
        )
        self.conn.commit()
        return self.get_password_reset_token(token_hash)

    def get_password_reset_token(self, token_hash: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT * FROM password_reset_tokens
            WHERE token_hash = ?
              AND used_at IS NULL
              AND expires_at > ?
            """,
            (token_hash, utc_now()),
        ).fetchone()
        return decode_row(row)

    def consume_password_reset_token(self, token_hash: str, password_hash: str) -> dict | None:
        token = self.get_password_reset_token(token_hash)
        if token is None:
            return None
        now = utc_now()
        self.conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now, token["user_id"]),
        )
        self.conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?",
            (now, token["id"]),
        )
        self.conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (now, token["user_id"]),
        )
        self.conn.commit()
        return self.get_user(token["user_id"])

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
        self.conn.commit()
        return self.get_profile(user_id, record_id)

    def list_profiles(self, user_id: str) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                "SELECT * FROM profiles WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
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
        self.conn.commit()
        return self.get_profile(user_id, record_id) if cur.rowcount else None

    def delete_profile(self, user_id: str, record_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM profiles WHERE id = ? AND user_id = ?", (record_id, user_id)
        )
        self.conn.commit()
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
        self.conn.commit()
        return self.get_score(user_id, record_id)

    def list_scores(self, user_id: str) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                "SELECT * FROM scores WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
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
        self.conn.commit()
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
        self.conn.commit()
        return self.get_plan(user_id, record_id)

    def list_plans(self, user_id: str, score_id: str | None = None) -> list[dict]:
        if score_id:
            rows = self.conn.execute(
                """
                SELECT * FROM plans
                WHERE user_id = ? AND score_id = ?
                ORDER BY created_at DESC
                """,
                (user_id, score_id),
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM plans WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
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
        self.conn.commit()
        return self.get_plan(user_id, record_id) if cur.rowcount else None

    def delete_plan(self, user_id: str, record_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM plans WHERE id = ? AND user_id = ?", (record_id, user_id)
        )
        self.conn.commit()
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
        self.conn.commit()
        return self.get_arrangement(user_id, record_id)

    def list_arrangements(self, user_id: str) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                """
                SELECT * FROM arrangements
                WHERE user_id = ?
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
        ]

    def get_arrangement(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM arrangements WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        ).fetchone()
        return decode_row(row)

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
        self.conn.commit()
        return self.get_run(user_id, record_id)

    def list_runs(self, user_id: str) -> list[dict]:
        return [
            decode_row(row)
            for row in self.conn.execute(
                "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        ]

    def get_run(self, user_id: str, record_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?", (record_id, user_id)
        ).fetchone()
        return decode_row(row)
