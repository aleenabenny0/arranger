"""Every multi-write operation is atomic, proven by failure injection.

Each test forces a later write inside one operation to raise, then checks from
a *fresh* connection that nothing the earlier writes did was kept. Reading on
the same connection would not prove anything: uncommitted changes are visible
to the connection that made them.

The tests run on SQLite always, and on Postgres whenever `TEST_DATABASE_URL`
points at a disposable database (the CI Postgres job sets it). Each Postgres
test gets its own schema, created and dropped around it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import PASSWORD, PLAN, PROFILE, SCORE, build_settings, csrf_headers, make_client, register  # noqa: E402
from test_postgres_integration import with_query_param  # noqa: E402

from arranger.adapters.score_json import score_to_dict  # noqa: E402
from arranger.ir import Note, Score, TrackInfo  # noqa: E402
from arranger.profile import PlayerProfile  # noqa: E402
from arranger.timeline import KeyChange, MeterChange, TempoChange, Timeline  # noqa: E402
from arranger_api.jobs import JobContext, JobServices, run_arrange, run_transcribe  # noqa: E402
from arranger_api.storage import Storage, connect, init_db  # noqa: E402
from arranger_api.storage.database import Database, PostgresConnection, connect_postgres  # noqa: E402
from arranger_api.storage.workspace import Workspace  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover - the api extra is installed for these tests
    psycopg = None

POSTGRES_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("POSTGRES_TEST_DATABASE_URL")
SKIP_POSTGRES = "TEST_DATABASE_URL is not set; the Postgres variant runs in the CI Postgres job"


# --- failure injection -------------------------------------------------------


class InjectedFailure(RuntimeError):
    """Raised in place of a write the test chose to break."""


class FailingConnection:
    """A connection that raises on the write matching `pattern`, after `skip` matches.

    Everything else is delegated to the real connection, so the code under
    test cannot tell the difference until the moment the failure is injected.
    The failure happens *instead of* the statement, on a healthy connection,
    which is exactly what a rollback has to cope with.
    """

    def __init__(self, conn, *, pattern: str, skip: int = 0):
        self._conn = conn
        self._pattern = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        self._skip = skip
        self.matches = 0

    def execute(self, sql, params=None):
        if self._pattern.search(sql):
            self.matches += 1
            if self.matches > self._skip:
                raise InjectedFailure("injected failure on: " + " ".join(sql.split())[:80])
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class Db:
    """One disposable database: a SQLite file or a private Postgres schema."""

    def __init__(self, dialect: str, *, sqlite_path: Path | None = None, url: str | None = None):
        self.dialect, self.sqlite_path, self.url = dialect, sqlite_path, url
        self.opened: list = []

    def connect(self):
        conn = connect_postgres(self.url) if self.dialect == "postgres" else connect(self.sqlite_path)
        self.opened.append(conn)
        return conn

    def storage(self) -> Storage:
        """A fresh connection: what a later request, or another process, would see."""
        return Storage(self.connect(), dialect=self.dialect)

    def failing(self, pattern: str, *, skip: int = 0) -> tuple[FailingConnection, Storage]:
        proxy = FailingConnection(self.connect(), pattern=pattern, skip=skip)
        return proxy, Storage(proxy, dialect=self.dialect)

    def database(self) -> Database:
        """A `Database` (the pooled facade the app and workers use) for this database."""
        if self.dialect == "postgres":
            return Database(url=self.url, pool_size=3)
        return Database(sqlite_path=self.sqlite_path)

    def proxied_database(self, pattern: str, *, skip: int = 0) -> Database:
        """A `Database` whose every connection injects the failure."""
        database = self.database()
        real_acquire, real_release = database.acquire, database.release

        def acquire():
            return FailingConnection(real_acquire(), pattern=pattern, skip=skip)

        def release(conn, *, discard: bool = False):
            real_release(conn._conn if isinstance(conn, FailingConnection) else conn, discard=discard)

        database.acquire, database.release = acquire, release
        return database

    def settings(self, tmp_path: Path):
        return build_settings(
            sqlite_path=str(self.sqlite_path or tmp_path / "unused.db"),
            database_url=self.url or "",
            artifact_backend="local",
            artifact_dir=str(tmp_path / "files"),
            job_workers=0,
        )

    def close(self) -> None:
        for conn in self.opened:
            try:
                conn.close()
            except Exception:
                pass
        self.opened.clear()


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    if request.param == "sqlite":
        path = tmp_path / "transactions.db"
        conn = connect(path)
        init_db(conn)
        conn.close()
        handle = Db("sqlite", sqlite_path=path)
        yield handle
        handle.close()
        return

    if not POSTGRES_URL or psycopg is None:
        pytest.skip(SKIP_POSTGRES)
    schema = f"arranger_tx_{uuid.uuid4().hex}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    url = with_query_param(POSTGRES_URL, "options", f"-c search_path={schema}")
    conn = connect_postgres(url)
    init_db(conn)
    conn.close()
    handle = Db("postgres", url=url)
    try:
        yield handle
    finally:
        # Open transactions hold locks that would make DROP SCHEMA wait forever.
        handle.close()
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# --- fixtures for rows -----------------------------------------------------------


def make_user(storage: Storage, email: str = "tx@example.com") -> str:
    return storage.create_user(email, "hash-original", "Tx User")["id"]


def small_score() -> Score:
    timeline = Timeline([TempoChange(0, 120)], [MeterChange(1, 4, 4)], [KeyChange(0, 0)])
    notes = []
    for bar in range(2):
        for k in range(4):
            beat = bar * 4 + k
            notes.append(Note(72 + k, beat * 0.5, 0.5, staff=1, bar=bar + 1, beat=float(beat), beats=1.0, track=0))
            notes.append(Note(48 + (0, 7, 4, 7)[k], beat * 0.5, 0.5, staff=2, bar=bar + 1, beat=float(beat),
                              beats=1.0, track=1))
    return Score(notes=notes, tempo_bpm=120, title="Two bars", timeline=timeline,
                 tracks=[TrackInfo(0, "Melody"), TrackInfo(1, "Accompaniment")])


def make_project(ws: Workspace, user_id: str, *, upload: dict | None = None) -> dict:
    score = small_score()
    return ws.create_project(
        user_id, title="Two bars", composer="", kind="midi", filename="two.mid",
        score=score_to_dict(score), note_count=len(score.notes), bar_count=2, inspection={"k": 1}, upload=upload,
    )


def count(storage: Storage, table: str, where: str = "1 = 1", params: tuple = ()) -> int:
    return int(storage.conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params).fetchone()["n"])


class MemoryStore:
    """An artifact store the tests can inspect."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def put(self, key, data, content_type):
        self.blobs[key] = bytes(data)

    def get(self, key):
        return self.blobs[key]

    def delete(self, key):
        self.blobs.pop(key, None)

    def exists(self, key):
        return key in self.blobs

    def healthy(self):
        return True


# --- accounts -------------------------------------------------------------------------


def test_password_reset_confirm_is_all_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    setup.create_session(user_id, "session-a", days=30)
    setup.create_password_reset_token(user_id, "reset-hash", 30)

    # Writes in order: spend the token, set the password, spend siblings, revoke sessions.
    _, storage = db.failing(r"UPDATE sessions")
    with pytest.raises(InjectedFailure):
        storage.consume_password_reset_token("reset-hash", "hash-new")

    check = db.storage()
    assert check.get_user_with_password_by_id(user_id)["password_hash"] == "hash-original"
    assert check.get_password_reset_token("reset-hash") is not None, "the token was spent and not given back"
    assert len(check.list_sessions(user_id)) == 1


def test_email_verification_is_all_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    setup.create_email_verification_token(user_id, "verify-hash", 60)

    _, storage = db.failing(r"UPDATE users")
    with pytest.raises(InjectedFailure):
        storage.consume_email_verification_token("verify-hash")

    check = db.storage()
    assert check.get_user(user_id)["email_verified_at"] is None
    assert check.get_email_verification_token("verify-hash") is not None


def test_change_password_is_all_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    setup.create_session(user_id, "session-a", days=30)

    _, storage = db.failing(r"UPDATE sessions")
    with pytest.raises(InjectedFailure):
        storage.change_password(user_id, "hash-new")

    check = db.storage()
    assert check.get_user_with_password_by_id(user_id)["password_hash"] == "hash-original"
    assert len(check.list_sessions(user_id)) == 1


def test_new_session_does_not_keep_its_housekeeping_when_the_insert_fails(db):
    setup = db.storage()
    user_id = make_user(setup)
    setup.create_session(user_id, "expired-token", days=0)     # expires now: pruned by the next create

    _, storage = db.failing(r"INSERT INTO sessions")
    with pytest.raises(InjectedFailure):
        storage.create_session(user_id, "new-token", days=30, max_sessions=5)

    check = db.storage()
    row = check.conn.execute("SELECT revoked_at FROM sessions WHERE token_hash = ?", ("expired-token",)).fetchone()
    assert row["revoked_at"] is None, "pruning the expired session was committed without the new one"
    assert check.session_for_token("new-token") is None


def test_register_over_http_leaves_no_user_when_the_session_write_fails(db):
    _, storage = db.failing(r"INSERT INTO sessions")
    with make_client(storage, raise_server_exceptions=False) as api:
        response = api.post("/auth/register", json={"email": "ghost@example.com", "password": PASSWORD,
                                                    "display_name": "Ghost"})
        assert response.status_code == 500
        assert "arranger_session" not in response.cookies

    check = db.storage()
    assert check.get_user_with_password("ghost@example.com") is None


def test_candidate_rankings_are_written_together(db):
    setup = db.storage()
    user_id = make_user(setup)
    profile = setup.create_profile(user_id, PROFILE)
    score = setup.create_score(user_id, SCORE)
    run = setup.create_run(user_id=user_id, score_id=score["id"], profile_id=profile["id"],
                           result={"accepted": True, "best_cost": 0.0})
    row = {"region_index": 0, "start_bar": 1, "end_bar": 1, "pattern": "block", "voices": 2,
           "melody_fold_window": 0, "difficulty_score": 1.0, "difficulty_rank": "easy", "energy": "low",
           "role": "verse support", "verifier_cost": 0.0, "planner_penalty": 0.0, "chosen": True}

    _, storage = db.failing(r"INSERT INTO candidate_rankings", skip=1)   # the second row fails
    with pytest.raises(InjectedFailure):
        storage.create_candidate_rankings(user_id=user_id, run_id=run["id"], score_id=score["id"],
                                          profile_id=profile["id"], rows=[row, dict(row, region_index=1)])

    check = db.storage()
    assert check.list_candidate_rankings(user_id, run_id=run["id"]) == []


def test_cleanup_is_all_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    setup.create_session(user_id, "dead-token", days=0)
    setup.create_password_reset_token(user_id, "spent-hash", 30)
    setup.consume_password_reset_token("spent-hash", "hash-two")

    _, storage = db.failing(r"DELETE FROM password_reset_tokens")
    with pytest.raises(InjectedFailure):
        storage.cleanup_expired()

    check = db.storage()
    assert count(check, "sessions", "token_hash = ?", ("dead-token",)) == 1
    assert count(check, "password_reset_tokens", "token_hash = ?", ("spent-hash",)) == 1


# --- projects, revisions, jobs ---------------------------------------------------------------


def test_create_project_writes_project_upload_and_source_together(db):
    setup = db.storage()
    user_id = make_user(setup)
    upload = {"kind": "upload", "filename": "two.mid", "content_type": "audio/midi", "storage_key": "u/1",
              "size_bytes": 3, "sha256": "abc", "expires_at": None}

    _, storage = db.failing(r"INSERT INTO project_sources")
    with pytest.raises(InjectedFailure):
        make_project(Workspace(storage), user_id, upload=upload)

    check = db.storage()
    assert Workspace(check).list_projects(user_id) == ([], 0)
    assert count(check, "artifacts") == 0


def test_source_revision_is_all_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    project = make_project(Workspace(setup), user_id)

    _, storage = db.failing(r"UPDATE projects SET current_source_id")
    with pytest.raises(InjectedFailure):
        Workspace(storage).add_source_revision(user_id, project["id"], based_on=project["current_source_id"],
                                               selection={"melody_track": 0}, inspection={})

    check_ws = Workspace(db.storage())
    assert len(check_ws.list_sources(user_id, project["id"])) == 1
    assert check_ws.get_project(user_id, project["id"])["current_source_id"] == project["current_source_id"]


def test_arrangement_revision_is_all_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    project = make_project(Workspace(setup), user_id)

    _, storage = db.failing(r"UPDATE projects SET current_arrangement_id")
    with pytest.raises(InjectedFailure):
        Workspace(storage).add_arrangement(
            user_id, project["id"], project["current_source_id"], origin="deterministic", accepted=True,
            n_hard=0, n_strain=0, fidelity_score=1.0, difficulty=1.0, algorithm_version="t", model=None,
            profile={}, plan={}, arranged={}, verdict={}, summary={}, report={},
        )

    check_ws = Workspace(db.storage())
    assert check_ws.list_arrangements(user_id, project["id"]) == []
    assert check_ws.get_project(user_id, project["id"])["current_arrangement_id"] is None


def test_deleting_a_project_removes_everything_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    ws = Workspace(setup)
    project = make_project(ws, user_id)
    ws.create_artifact(user_id, project_id=project["id"], source_id=None, arrangement_id=None, kind="midi",
                       filename="a.mid", content_type="audio/midi", storage_key="k/1", size_bytes=1, sha256="x")
    ws.create_job(user_id, kind="arrange", project_id=project["id"], payload={})

    # The last statement of the cascade is the project row itself.
    _, storage = db.failing(r"DELETE FROM projects\s+WHERE")
    with pytest.raises(InjectedFailure):
        Workspace(storage).delete_project(user_id, project["id"])

    check = db.storage()
    assert Workspace(check).get_project(user_id, project["id"]) is not None
    assert count(check, "project_sources", "project_id = ?", (project["id"],)) == 1
    assert count(check, "artifacts", "project_id = ?", (project["id"],)) == 1
    assert count(check, "jobs", "project_id = ?", (project["id"],)) == 1


def test_deleting_an_account_removes_everything_or_nothing(db):
    setup = db.storage()
    user_id = make_user(setup)
    setup.create_session(user_id, "session-a", days=30)
    make_project(Workspace(setup), user_id)

    _, storage = db.failing(r"DELETE FROM users")
    with pytest.raises(InjectedFailure):
        Workspace(storage).delete_user(user_id)

    check = db.storage()
    assert check.get_user(user_id) is not None
    assert len(check.list_sessions(user_id)) == 1
    assert count(check, "projects", "user_id = ?", (user_id,)) == 1


def test_recovering_lost_jobs_fails_and_requeues_together(db):
    setup = db.storage()
    user_id = make_user(setup)
    ws = Workspace(setup)
    exhausted, _ = ws.create_job(user_id, kind="arrange", payload={}, max_attempts=2)
    retryable, _ = ws.create_job(user_id, kind="arrange", payload={}, max_attempts=2)
    past = "2000-01-01T00:00:00.000000+00:00"
    for job, attempts in ((exhausted, 2), (retryable, 1)):
        setup.conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = 'dead', lease_expires_at = ?, attempts = ? WHERE id = ?",
            (past, attempts, job["id"]),
        )
    setup.conn.commit()

    _, storage = db.failing(r"UPDATE jobs SET status = 'queued'")
    with pytest.raises(InjectedFailure):
        Workspace(storage).recover_expired_jobs()

    check_ws = Workspace(db.storage())
    assert check_ws.get_job(user_id, exhausted["id"])["status"] == "running", "failed without requeueing the other"
    assert check_ws.get_job(user_id, retryable["id"])["status"] == "running"


# --- job handlers: a database transaction plus files in the object store ----------------------


def test_transcription_result_links_the_upload_or_creates_no_project(db, tmp_path):
    setup = db.storage()
    user_id = make_user(setup)
    ws = Workspace(setup)
    upload = ws.create_artifact(user_id, project_id=None, source_id=None, arrangement_id=None, kind="upload",
                                filename="take.wav", content_type="audio/wav", storage_key="u/take", size_bytes=3,
                                sha256="x")
    ws.create_job(user_id, kind="transcribe", payload={"upload_artifact_id": upload["id"], "title": "Take"},
                  max_attempts=1)
    job = ws.claim_job("w1", 120)     # a handler only runs a job its worker holds the lease on
    store = MemoryStore()
    store.blobs["u/take"] = b"wav"

    def transcriber(data, filename, progress, should_cancel):
        return types.SimpleNamespace(score=small_score(), model="fake", overall_confidence=0.9,
                                     tempo_bpm=120.0, tempo_confidence=0.9, warnings=[])

    database = db.proxied_database(r"UPDATE artifacts SET project_id")
    services = JobServices(settings=db.settings(tmp_path), database=database, artifacts=store, transcriber=transcriber)
    try:
        with pytest.raises(InjectedFailure):
            run_transcribe(JobContext(job, services, "w1", time.monotonic() + 60))
    finally:
        database.close()

    check = db.storage()
    assert Workspace(check).list_projects(user_id) == ([], 0)
    assert check.conn.execute("SELECT project_id FROM artifacts WHERE id = ?", (upload["id"],)).fetchone()["project_id"] is None


def test_saving_an_arrangement_keeps_no_row_and_no_file_when_a_file_row_fails(db, tmp_path):
    setup = db.storage()
    user_id = make_user(setup)
    ws = Workspace(setup)
    project = make_project(ws, user_id)
    payload = {"project_id": project["id"], "source_id": project["current_source_id"],
               "profile": PlayerProfile().to_dict(), "use_model": False, "plan": None, "label": ""}
    ws.create_job(user_id, kind="arrange", project_id=project["id"], payload=payload, max_attempts=1)
    job = ws.claim_job("w1", 120)     # progress reports check the lease; an unclaimed job reads as cancelled
    store = MemoryStore()

    # The MIDI row is written; the MusicXML row is the second artifact insert and fails.
    database = db.proxied_database(r"INSERT INTO artifacts", skip=1)
    services = JobServices(settings=db.settings(tmp_path), database=database, artifacts=store)
    try:
        with pytest.raises(InjectedFailure):
            run_arrange(JobContext(job, services, "w1", time.monotonic() + 120))
    finally:
        database.close()

    check = db.storage()
    assert Workspace(check).list_arrangements(user_id, project["id"]) == []
    assert count(check, "artifacts") == 0
    assert store.blobs == {}, "the MIDI bytes were written to the store and never cleaned up"


# --- nesting --------------------------------------------------------------------------------------


def test_an_inner_failure_rolls_back_the_outer_block_even_when_swallowed(db):
    storage = db.storage()
    user_id = make_user(storage)
    with storage.transaction():
        storage.create_profile(user_id, PROFILE)
        try:
            with storage.transaction():
                storage.create_score(user_id, SCORE)
                raise RuntimeError("inner failure the caller catches")
        except RuntimeError:
            pass
    check = db.storage()
    assert check.list_profiles(user_id) == []
    assert check.list_scores(user_id) == []


# --- streaming export -----------------------------------------------------------------------------


def make_legacy_arrangements(storage: Storage, user_id: str, how_many: int) -> list[str]:
    profile = storage.create_profile(user_id, PROFILE)
    score = storage.create_score(user_id, SCORE)
    plan = storage.create_plan(user_id, score["id"], PLAN)
    ids = []
    for _ in range(how_many):
        record = storage.create_arrangement(
            user_id=user_id, score_id=score["id"], plan_id=plan["id"], profile_id=profile["id"], arranged=SCORE,
            verdict={"playable": True, "n_hard": 0, "n_strain": 0}, fidelity={"score": 1.0},
        )
        ids.append(record["id"])
    return ids


def test_iter_arrangements_streams_every_row_in_order_in_batches(db, monkeypatch):
    setup = db.storage()
    user_id = make_user(setup)
    other_id = make_user(setup, "other@example.com")
    ids = make_legacy_arrangements(setup, user_id, 5)
    make_legacy_arrangements(setup, other_id, 1)

    cursor_names: list = []
    if db.dialect == "postgres":
        original = PostgresConnection.cursor

        def spy(self, name=None, **kwargs):
            cursor_names.append(name)
            return original(self, name, **kwargs)

        monkeypatch.setattr(PostgresConnection, "cursor", spy)

    storage = db.storage()
    fetched = list(storage.iter_arrangements(user_id, batch_size=2))
    assert [r["id"] for r in fetched] == ids
    assert all(r["verdict"]["playable"] is True for r in fetched), "JSON columns are decoded"
    if db.dialect == "postgres":
        assert cursor_names and cursor_names[0], "Postgres must stream through a named server-side cursor"
        # The named cursor is closed before the transaction ends, so the connection is reusable.
        assert storage.ping()


def test_export_route_streams_ndjson_for_the_signed_in_user_only(db, tmp_path):
    settings = db.settings(tmp_path)
    with make_client(settings=settings) as api:
        register(api, email="exporter@example.com")
        headers = csrf_headers(api)
        profile = api.post("/profiles", json=PROFILE, headers=headers).json()["record"]
        score = api.post("/scores", json=SCORE, headers=headers).json()["record"]
        plan = api.post("/plans", json={"score_id": score["id"], "plan": PLAN}, headers=headers).json()["record"]
        ids = []
        for _ in range(3):
            response = api.post("/arrangements/render-and-verify",
                                json={"score_id": score["id"], "profile_id": profile["id"], "plan_id": plan["id"]},
                                headers=headers)
            assert response.status_code == 200, response.text
            ids.append(response.json()["record"]["id"])

        response = api.get("/arrangements/export")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert "attachment" in response.headers["content-disposition"]
        records = [json.loads(line) for line in response.text.splitlines() if line]
        assert [r["id"] for r in records] == ids
        assert records[0]["verdict"]["playable"] is True
        # Each streamed line is exactly the record the single-record route returns.
        assert records[0] == api.get(f"/arrangements/{ids[0]}").json()["record"]
        # The next request on the same client still works: the stream released its connection.
        assert api.get("/arrangements").status_code == 200

    with make_client(settings=settings) as other:
        register(other, email="nobody@example.com")
        assert other.get("/arrangements/export").text == ""

    with make_client(settings=settings) as anonymous:
        assert anonymous.get("/arrangements/export").status_code == 401


# --- the cursor wrapper, without a server -----------------------------------------------------------


def test_postgres_cursor_wrapper_translates_placeholders_and_names_server_cursors():
    executed: list = []
    created: list = []

    class FakeCursor:
        itersize = None

        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchmany(self, size):
            return [{"n": size}]

        def close(self):
            executed.append("closed")

    class FakeRaw:
        def cursor(self, name=None):
            created.append(name)
            return FakeCursor()

    conn = PostgresConnection(FakeRaw())
    with conn.cursor(name="stream", itersize=50) as cursor:
        cursor.execute("SELECT * FROM t WHERE a = ? AND b = ?", (1, 2))
        assert cursor.fetchmany(7) == [{"n": 7}]
    assert created == ["stream"]
    assert executed[0] == ("SELECT * FROM t WHERE a = %s AND b = %s", (1, 2))
    assert executed[-1] == "closed"
    conn.cursor()
    assert created == ["stream", None]
