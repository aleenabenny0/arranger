"""Storage repository tests."""

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger_api.storage import Storage, connect, init_db  # noqa: E402


PROFILE = {
    "name": "storage",
    "instrument": "piano",
    "lowest_pitch": 21,
    "highest_pitch": 108,
    "max_span": 12,
    "comfortable_span": 9,
    "max_notes_per_hand": 5,
    "max_leap_rate": 70.0,
    "leap_slack": 5,
    "skill_level": 4,
}

SCORE = {
    "title": "storage score",
    "tempo_bpm": 100.0,
    "notes": [
        {"pitch": 60, "onset": 0.0, "duration": 1.0, "staff": None, "bar": 1, "voice": 1}
    ],
}

PLAN = {
    "title": "storage plan",
    "target_skill": 4,
    "sections": [
        {
            "start_bar": 1,
            "end_bar": 1,
            "lh_pattern": "pedal_tone",
            "melody_shift": 0,
            "lh_octave": 3,
            "lh_voices": 1,
            "roll_wide_chords": False,
            "melody_fold_window": 0,
            "label": "",
        }
    ],
    "reductions": [],
    "pedal_bars": [],
    "notes": "",
}


def make_storage() -> Storage:
    conn = connect(":memory:")
    init_db(conn)
    return Storage(conn)


def make_user(storage: Storage, email: str = "storage@example.com") -> str:
    return storage.create_user(email, "hash", "Storage User")["id"]


def test_profile_crud():
    storage = make_storage()
    user_id = make_user(storage)
    created = storage.create_profile(user_id, PROFILE)
    assert created["payload"]["name"] == "storage"

    updated = dict(PROFILE, name="updated")
    record = storage.update_profile(user_id, created["id"], updated)
    assert record["payload"]["name"] == "updated"

    assert storage.delete_profile(user_id, created["id"])
    assert storage.get_profile(user_id, created["id"]) is None


def test_score_plan_arrangement_flow():
    storage = make_storage()
    user_id = make_user(storage)
    profile = storage.create_profile(user_id, PROFILE)
    score = storage.create_score(user_id, SCORE)
    plan = storage.create_plan(user_id, score["id"], PLAN)
    arrangement = storage.create_arrangement(
        user_id=user_id,
        score_id=score["id"],
        plan_id=plan["id"],
        profile_id=profile["id"],
        arranged=SCORE,
        verdict={"playable": True, "n_hard": 0, "n_strain": 0},
        fidelity={"score": 1.0},
    )

    assert arrangement["score_id"] == score["id"]
    assert arrangement["verdict"]["playable"]
    assert storage.get_arrangement(user_id, arrangement["id"])["fidelity"]["score"] == 1.0


def test_candidate_rankings_are_persisted_and_user_scoped():
    storage = make_storage()
    user_id = make_user(storage)
    other_id = make_user(storage, "candidate-other@example.com")
    profile = storage.create_profile(user_id, PROFILE)
    score = storage.create_score(user_id, SCORE)
    run = storage.create_run(
        user_id=user_id,
        score_id=score["id"],
        profile_id=profile["id"],
        result={"accepted": True, "best_cost": 0.0},
    )

    created = storage.create_candidate_rankings(
        user_id=user_id,
        run_id=run["id"],
        score_id=score["id"],
        profile_id=profile["id"],
        rows=[
            {
                "region_index": 0,
                "start_bar": 1,
                "end_bar": 1,
                "pattern": "pedal_tone",
                "voices": 1,
                "melody_fold_window": 0,
                "difficulty_score": 2.0,
                "difficulty_rank": "moderate",
                "energy": "low",
                "role": "verse support",
                "note_count": 1,
                "average_density": 1.0,
                "max_simultaneous": 1,
                "root_changes": 0,
                "melody_span": 0,
                "max_melody_leap": 0,
                "tempo_bpm": 100.0,
                "profile_fit": 0.5,
                "verifier_cost": 0.0,
                "planner_penalty": 0.1,
                "chosen": True,
            }
        ],
    )

    assert created[0]["payload"]["chosen"]
    assert storage.list_candidate_rankings(user_id, run_id=run["id"])
    assert storage.list_candidate_rankings(other_id) == []


def test_deleting_score_cascades_plans_and_arrangements():
    storage = make_storage()
    user_id = make_user(storage)
    profile = storage.create_profile(user_id, PROFILE)
    score = storage.create_score(user_id, SCORE)
    plan = storage.create_plan(user_id, score["id"], PLAN)
    arrangement = storage.create_arrangement(
        user_id=user_id,
        score_id=score["id"],
        plan_id=plan["id"],
        profile_id=profile["id"],
        arranged=SCORE,
        verdict={"playable": True, "n_hard": 0, "n_strain": 0},
        fidelity={"score": 1.0},
    )

    assert storage.delete_score(user_id, score["id"])
    assert storage.get_plan(user_id, plan["id"]) is None
    assert storage.get_arrangement(user_id, arrangement["id"]) is None


def test_users_cannot_read_each_others_scores():
    storage = make_storage()
    owner_id = make_user(storage, "owner@example.com")
    other_id = make_user(storage, "other@example.com")
    score = storage.create_score(owner_id, SCORE)

    assert storage.get_score(owner_id, score["id"]) is not None
    assert storage.get_score(other_id, score["id"]) is None


def test_revoke_user_sessions_revokes_only_that_user():
    storage = make_storage()
    user_id = make_user(storage, "sessions@example.com")
    other_id = make_user(storage, "other-sessions@example.com")
    storage.create_session(user_id, "token-one")
    storage.create_session(user_id, "token-two")
    storage.create_session(other_id, "token-three")

    revoked = storage.revoke_user_sessions(user_id)

    assert revoked == 2
    assert storage.session_for_token("token-one") is None
    assert storage.session_for_token("token-two") is None
    assert storage.session_for_token("token-three") is not None


def test_score_lists_support_limit_and_offset():
    storage = make_storage()
    user_id = make_user(storage, "pages@example.com")
    for index in range(3):
        storage.create_score(user_id, dict(SCORE, title=f"score {index}"))

    first_page = storage.list_scores(user_id, limit=2, offset=0)
    second_page = storage.list_scores(user_id, limit=2, offset=2)

    assert len(first_page) == 2
    assert len(second_page) == 1


def test_migrations_are_recorded():
    conn = connect(":memory:")
    init_db(conn)
    migrations = {row["id"] for row in conn.execute("SELECT id FROM schema_migrations")}

    assert "0001_initial_storage" in migrations
    assert "0002_auth_hardening" in migrations
    assert "0003_auth_schema_backfill" in migrations
    assert "0004_candidate_rankings" in migrations


def test_legacy_user_schema_is_backfilled():
    conn = connect(":memory:")
    conn.execute(
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO users (id, email) VALUES (?, ?)",
        ("legacy-user", "legacy@example.com"),
    )

    init_db(conn)

    storage = Storage(conn)
    assert storage.ping()
    user = storage.get_user_with_password("legacy@example.com")
    assert user["password_hash"] == "unusable_password_hash"
    created = storage.create_user("new@example.com", "hash", "New User")
    assert created["email"] == "new@example.com"


def test_new_migrations_are_recorded_and_status_is_reported():
    conn = connect(":memory:")
    assert Storage(conn).migration_status()["current"] is None
    from arranger_api.storage.migrations import MIGRATIONS

    applied = init_db(conn)
    assert applied == [m.id for m in MIGRATIONS], "applied in order, none skipped"
    assert applied[5:9] == [
        "0006_rate_limit_buckets", "0007_email_verification", "0008_projects_jobs_artifacts",
        "0009_job_dispatch",
    ]
    assert init_db(conn) == []  # idempotent

    status = Storage(conn).migration_status()
    assert status["current"] == MIGRATIONS[-1].id
    assert status["pending"] == []
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"projects", "project_sources", "project_arrangements", "artifacts", "jobs", "artifact_blobs"} <= tables
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    assert "email_verified_at" in columns


# --- transactions -------------------------------------------------------------


def test_transaction_commits_on_success_and_rolls_back_on_error():
    storage = make_storage()
    with storage.transaction():
        user_id = make_user(storage, "tx-ok@example.com")
        storage.create_profile(user_id, PROFILE)
    assert storage.get_user_with_password("tx-ok@example.com") is not None

    with pytest.raises(RuntimeError):
        with storage.transaction():
            doomed = make_user(storage, "tx-fail@example.com")
            storage.create_profile(doomed, PROFILE)
            raise RuntimeError("boom")
    assert storage.get_user_with_password("tx-fail@example.com") is None
    assert not storage.conn.in_transaction

    # Near miss: outside a transaction each call still commits on its own.
    make_user(storage, "autocommit@example.com")
    storage.conn.rollback()
    assert storage.get_user_with_password("autocommit@example.com") is not None


def test_nested_transaction_failure_rolls_back_the_outer_one_even_if_swallowed():
    storage = make_storage()
    with storage.transaction():
        make_user(storage, "outer@example.com")
        try:
            with storage.transaction():
                make_user(storage, "inner@example.com")
                raise ValueError("inner failure")
        except ValueError:
            pass
    assert storage.get_user_with_password("outer@example.com") is None
    assert storage.get_user_with_password("inner@example.com") is None


def test_transaction_is_not_visible_to_other_connections_until_commit(tmp_path):
    path = tmp_path / "isolation.db"
    writer = Storage(connect(path))
    init_db(writer.conn)
    reader = Storage(connect(path))
    try:
        with writer.transaction():
            make_user(writer, "pending@example.com")
            assert reader.get_user_with_password("pending@example.com") is None
        assert reader.get_user_with_password("pending@example.com") is not None
    finally:
        writer.conn.close()
        reader.conn.close()


# --- 9: atomic single-use tokens ----------------------------------------------


def test_reset_token_consumption_is_atomic_under_concurrency(tmp_path):
    # Audit 9: SELECT then unconditional UPDATE let several simultaneous
    # confirms all succeed. Before the fix this test saw 7 of 8 winners.
    path = tmp_path / "race.db"
    setup = Storage(connect(path))
    init_db(setup.conn)
    user_id = make_user(setup, "race@example.com")
    setup.create_session(user_id, "live-session")
    setup.create_password_reset_token(user_id, "reset-token-hash", 30)
    setup.conn.close()

    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list = []

    def confirm(index: int) -> None:
        storage = Storage(connect(path))
        try:
            barrier.wait(timeout=10)
            user = storage.consume_password_reset_token("reset-token-hash", f"new-hash-{index}")
            outcomes.append(("won", index) if user is not None else ("lost", index))
        except Exception as exc:  # a lock error would be a failure of the design
            outcomes.append(("error", repr(exc)))
        finally:
            storage.conn.close()

    threads = [threading.Thread(target=confirm, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    winners = [index for result, index in outcomes if result == "won"]
    assert len(outcomes) == workers
    assert [o for o in outcomes if o[0] == "error"] == []
    assert len(winners) == 1

    check = Storage(connect(path))
    try:
        # The winner's password is the one stored, and its sessions were revoked
        # in the same transaction.
        stored = check.get_user_with_password("race@example.com")["password_hash"]
        assert stored == f"new-hash-{winners[0]}"
        assert check.session_for_token("live-session") is None
    finally:
        check.conn.close()


def test_reset_token_rules_used_expired_and_unknown():
    storage = make_storage()
    user_id = make_user(storage, "rules@example.com")
    storage.create_session(user_id, "session-a")

    assert storage.consume_password_reset_token("never-issued", "h") is None

    storage.create_password_reset_token(user_id, "expired", 30)
    storage.conn.execute(
        "UPDATE password_reset_tokens SET expires_at = ? WHERE token_hash = 'expired'",
        ("2000-01-01T00:00:00.000000+00:00",),
    )
    storage.conn.commit()
    assert storage.consume_password_reset_token("expired", "h") is None
    # Near miss: a failed attempt changes nothing.
    assert storage.get_user_with_password("rules@example.com")["password_hash"] == "hash"
    assert storage.session_for_token("session-a") is not None

    storage.create_password_reset_token(user_id, "good", 30)
    storage.create_password_reset_token(user_id, "sibling", 30)
    assert storage.consume_password_reset_token("good", "new-hash")["id"] == user_id
    assert storage.consume_password_reset_token("good", "again") is None
    assert storage.consume_password_reset_token("sibling", "again") is None  # retired with it
    assert storage.get_user_with_password("rules@example.com")["password_hash"] == "new-hash"
    assert storage.session_for_token("session-a") is None


def test_reset_consumption_rolls_back_as_a_unit():
    storage = make_storage()
    user_id = make_user(storage, "unit@example.com")
    storage.create_session(user_id, "session-unit")
    storage.create_password_reset_token(user_id, "unit-token", 30)

    class Exploding:
        """Fails on the session-revocation statement, the last of the three."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args):
            if "UPDATE sessions" in sql:
                raise sqlite3.OperationalError("simulated failure")
            return self._conn.execute(sql, *args)

    real = storage.conn
    storage.conn = Exploding(real)
    with pytest.raises(sqlite3.OperationalError):
        storage.consume_password_reset_token("unit-token", "half-applied")
    storage.conn = real

    assert storage.get_user_with_password("unit@example.com")["password_hash"] == "hash"
    assert storage.get_password_reset_token("unit-token") is not None  # still spendable
    assert storage.consume_password_reset_token("unit-token", "applied") is not None


def test_email_verification_token_is_atomic_and_single_use(tmp_path):
    path = tmp_path / "verify.db"
    setup = Storage(connect(path))
    init_db(setup.conn)
    user_id = make_user(setup, "verify-race@example.com")
    setup.create_email_verification_token(user_id, "verify-hash", 60)
    assert setup.get_user(user_id)["email_verified_at"] is None
    setup.conn.close()

    barrier = threading.Barrier(6)
    wins: list[bool] = []

    def verify() -> None:
        storage = Storage(connect(path))
        try:
            barrier.wait(timeout=10)
            wins.append(storage.consume_email_verification_token("verify-hash") is not None)
        finally:
            storage.conn.close()

    threads = [threading.Thread(target=verify) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert wins.count(True) == 1 and len(wins) == 6

    check = Storage(connect(path))
    try:
        assert check.get_user(user_id)["email_verified_at"] is not None
    finally:
        check.conn.close()


def test_create_session_prunes_trims_and_inserts_atomically():
    storage = make_storage()
    user_id = make_user(storage, "cap@example.com")
    for index in range(4):
        storage.create_session(user_id, f"token-{index}", max_sessions=2)
    live = [row["token_hash"] for row in storage.conn.execute(
        "SELECT token_hash FROM sessions WHERE revoked_at IS NULL ORDER BY created_at"
    )]
    assert live == ["token-2", "token-3"]


# --- concurrent migrations ----------------------------------------------------


def test_first_connections_to_a_new_file_can_race(tmp_path):
    # Switching a new file to WAL needs an exclusive lock that SQLite will not
    # wait for. A web process and a worker starting together must both succeed.
    for trial in range(8):
        path = tmp_path / f"first-open-{trial}.db"
        barrier = threading.Barrier(8)
        errors: list[str] = []

        def start(path=path, barrier=barrier, errors=errors) -> None:
            try:
                barrier.wait(timeout=10)
                conn = connect(path)
                try:
                    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                finally:
                    conn.close()
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=start) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == []


def test_concurrent_startups_apply_each_migration_exactly_once(tmp_path):
    path = tmp_path / "migrate-race.db"
    barrier = threading.Barrier(6)
    errors: list[str] = []
    applied: list[list[str]] = []

    def start() -> None:
        conn = connect(path)
        try:
            barrier.wait(timeout=10)
            applied.append(init_db(conn))
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    everything = [migration for batch in applied for migration in batch]
    assert sorted(everything) == sorted(set(everything))  # nobody applied one twice
    conn = connect(path)
    try:
        assert Storage(conn).migration_status()["pending"] == []
        assert Storage(conn).ping()
    finally:
        conn.close()


def test_postgres_migrations_take_an_advisory_lock():
    # No Postgres server here, so assert the statements the runner issues.
    from arranger_api.storage import migrations

    class Recorder:
        dialect = "postgres"

        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(" ".join(sql.split()))
            return self

        def fetchone(self):
            return {"now_value": "2026-01-01"}

        def __iter__(self):
            return iter([{"id": m.id} for m in migrations.MIGRATIONS])  # all applied

        def commit(self):
            self.statements.append("COMMIT")

        def rollback(self):
            self.statements.append("ROLLBACK")

    conn = Recorder()
    assert migrations.run_migrations(conn) == []
    assert conn.statements[0] == "SELECT pg_advisory_lock(?)"
    assert "SELECT pg_advisory_unlock(?)" in conn.statements
    assert conn.statements.index("SELECT pg_advisory_unlock(?)") > conn.statements.index(
        "SELECT id FROM schema_migrations"
    )


# --- connection pool ----------------------------------------------------------


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def make_pool(**kwargs):
    from arranger_api.storage import ConnectionPool

    created = []

    def connect_fake():
        conn = FakeConnection()
        created.append(conn)
        return conn

    def reset(conn):
        conn.rollback()
        return True

    pool = ConnectionPool(connect_fake, reset=reset, **kwargs)
    return pool, created


def test_pool_is_bounded_and_times_out_instead_of_growing():
    from arranger_api.storage import PoolTimeout

    pool, created = make_pool(max_size=2, timeout=0.05)
    first, second = pool.acquire(), pool.acquire()
    with pytest.raises(PoolTimeout):
        pool.acquire()
    assert len(created) == 2

    pool.release(first)
    assert pool.acquire() is first  # reused, not re-created
    assert first.rollbacks == 1  # and reset before reuse
    pool.release(first)
    pool.release(second)
    assert pool.stats().in_use == 0 and pool.stats().idle == 2


def test_pool_waiters_are_served_when_a_connection_is_released():
    pool, created = make_pool(max_size=1, timeout=5)
    held = pool.acquire()
    got = []
    waiter = threading.Thread(target=lambda: got.append(pool.acquire()))
    waiter.start()
    pool.release(held)
    waiter.join(timeout=5)
    assert got == [held] and len(created) == 1


def test_pool_retires_connections_past_their_lifetime_and_unhealthy_ones():
    now = [0.0]
    healthy = {"value": True}
    from arranger_api.storage import ConnectionPool

    created = []

    def connect_fake():
        created.append(FakeConnection())
        return created[-1]

    pool = ConnectionPool(
        connect_fake,
        max_size=2,
        max_lifetime=100,
        health_check_after=10,
        is_healthy=lambda conn: healthy["value"],
        clock=lambda: now[0],
    )
    conn = pool.acquire()
    pool.release(conn)

    now[0] = 5  # fresh: reused without a health check
    healthy["value"] = False
    assert pool.acquire() is conn
    pool.release(conn)

    now[0] = 30  # idle long enough to be checked, and the check fails
    replacement = pool.acquire()
    assert replacement is not conn and conn.closed
    pool.release(replacement)

    healthy["value"] = True
    now[0] = 500  # past max lifetime
    third = pool.acquire()
    assert third is not replacement and replacement.closed
    pool.release(third, discard=True)
    assert third.closed and pool.stats().idle == 0


def test_pool_releases_its_slot_when_connecting_fails():
    from arranger_api.storage import ConnectionPool

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("server unreachable")
        return FakeConnection()

    pool = ConnectionPool(flaky, max_size=1, timeout=0.05)
    with pytest.raises(OSError):
        pool.acquire()
    assert pool.acquire() is not None  # the slot was not leaked


def test_database_facade_uses_the_pool_for_postgres_urls(monkeypatch):
    import arranger_api.storage.database as database_module
    from arranger_api.storage import Database

    opened = []

    def fake_connect(url, *, statement_timeout_ms, connect_timeout=10):
        opened.append((url, statement_timeout_ms))
        conn = FakeConnection()
        conn.dialect = "postgres"
        return conn

    monkeypatch.setattr(database_module, "connect_postgres", fake_connect)
    database = Database(url="postgresql://u:p@db/x", pool_size=1, statement_timeout_ms=1234)
    assert database.dialect == "postgres"
    with database.connection() as first:
        pass
    with database.connection() as second:
        assert second is first
    assert opened == [("postgresql://u:p@db/x", 1234)]
    assert first.rollbacks >= 2  # rolled back on every release
    database.close()
    assert first.closed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
