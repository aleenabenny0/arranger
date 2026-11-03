"""Real Postgres integration test.

Set TEST_DATABASE_URL to a disposable Postgres database URL before running
this file (the older name POSTGRES_TEST_DATABASE_URL is still honoured). The
test creates and drops an isolated temporary schema. Without a URL the
database tests are skipped, with the reason shown in the pytest summary.
"""

import os
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import psycopg
except ImportError:
    psycopg = None

from arranger_api.storage import Database, Storage, connect, init_db  # noqa: E402
from arranger_api.storage.rate_limits import DatabaseRateLimitStore  # noqa: E402

SKIP_REASON = (
    "TEST_DATABASE_URL is not set; point it at a disposable Postgres database "
    "to run the Postgres integration tests"
)


def postgres_test_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("POSTGRES_TEST_DATABASE_URL")


PROFILE = {
    "name": "postgres player",
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
    "title": "postgres score",
    "tempo_bpm": 100.0,
    "notes": [
        {"pitch": 60, "onset": 0.0, "duration": 1.0, "staff": None, "bar": 1, "voice": 1}
    ],
}

PLAN = {
    "title": "postgres plan",
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


def with_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    encoded = urlencode(query, quote_via=quote)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, parts.fragment))


def with_env(values, fn):
    old = os.environ.copy()
    os.environ.update(values)
    try:
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(old)


def run_postgres_flow(url: str) -> None:
    if psycopg is None:
        raise RuntimeError("psycopg is not installed; run: pip install -e .[api]")

    schema = f"arranger_test_{uuid.uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')

    test_url = with_query_param(url, "options", f"-c search_path={schema}")
    try:
        def check():
            conn = connect()
            init_db(conn)
            try:
                storage = Storage(conn)
                user_id = storage.create_user(
                    f"{schema}@example.com",
                    "hash",
                    "Postgres User",
                )["id"]
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

                assert storage.get_user(user_id)["email"] == f"{schema}@example.com"
                assert storage.list_scores(user_id)[0]["payload"]["title"] == "postgres score"
                assert storage.get_arrangement(user_id, arrangement["id"])["verdict"]["playable"]
                assert storage.delete_score(user_id, score["id"])
                assert storage.get_plan(user_id, plan["id"]) is None
                assert init_db(conn) == []  # advisory-locked re-run is a no-op
                assert storage.migration_status()["pending"] == []
                assert storage.ping()

                with pytest.raises(RuntimeError):
                    with storage.transaction():
                        storage.create_user(f"rolled-back-{schema}@example.com", "h", "Gone")
                        raise RuntimeError("roll back")
                assert storage.get_user_with_password(f"rolled-back-{schema}@example.com") is None

                check_atomic_reset_token(test_url, storage, user_id)
                check_pool_and_rate_limit_store(test_url)
                check_project_workflow(test_url)
            finally:
                conn.close()

        with_env({"ARRANGER_DATABASE_URL": test_url}, check)
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def check_atomic_reset_token(test_url: str, storage: Storage, user_id: str) -> None:
    storage.create_password_reset_token(user_id, "pg-reset-hash", 30)
    database = Database(url=test_url, pool_size=6)
    barrier = threading.Barrier(6)
    wins: list[bool] = []

    def confirm(index: int) -> None:
        with database.connection() as conn:
            barrier.wait(timeout=10)
            user = Storage(conn).consume_password_reset_token("pg-reset-hash", f"hash-{index}")
            wins.append(user is not None)

    threads = [threading.Thread(target=confirm, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    database.close()
    assert len(wins) == 6 and wins.count(True) == 1


WORKFLOW_TABLES = ("projects", "project_sources", "project_arrangements", "artifacts", "jobs", "artifact_blobs")


def check_project_workflow(test_url: str) -> None:
    from conftest import build_settings

    def count_rows(table: str) -> int:
        with psycopg.connect(test_url) as conn:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    settings = build_settings(database_url=test_url, artifact_backend="database", job_workers=0, quota_active_jobs=5)
    run_project_workflow(settings, count_rows)


def test_the_project_workflow_check_itself_passes_on_sqlite(tmp_path):
    # The same journey CI runs against Postgres, here against SQLite, so a mistake in
    # the check is found on a machine with no Postgres server.
    import sqlite3

    from conftest import build_settings

    path = tmp_path / "workflow.db"

    def count_rows(table: str) -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            conn.close()

    settings = build_settings(sqlite_path=str(path), artifact_backend="database", job_workers=0, quota_active_jobs=5)
    run_project_workflow(settings, count_rows)


def run_project_workflow(settings, count_rows) -> None:
    """Upload, arrange, export, race the workers, export the account, delete it.

    SQLite runs this journey in test_api_projects.py. The differences that matter
    here are the ones SQLite cannot show: BYTEA artifact storage, row locking when
    several workers claim jobs at once, and cascading deletes.
    """
    import io
    import time
    import zipfile

    from conftest import PASSWORD, csrf_headers, make_client, register
    from test_api_projects import PROFILE as HANDS
    from test_api_projects import arrange, upload

    from arranger.io import read_midi_bytes

    with make_client(settings=settings) as api:
        email = f"pg-{uuid.uuid4().hex[:10]}@example.com"
        register(api, email=email)
        created = upload(api)
        assert created.status_code == 201, created.text
        project_id = created.json()["project"]["id"]

        arrangement_id = arrange(api, project_id, label="on postgres")
        record = api.get(f"/projects/{project_id}/arrangements/{arrangement_id}").json()
        assert record["arrangement"]["accepted"]

        exported = api.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/midi",
                            headers=csrf_headers(api))
        download = api.get(f"/artifacts/{exported.json()['artifact']['id']}/download")
        assert download.status_code == 200
        assert len(read_midi_bytes(download.content).notes) == len(record["playback"]["notes"])
        assert api.get("/projects", params={"q": "WALTZ"}).json()["total"] == 1
        assert api.get("/projects", params={"q": "100%_nothing"}).json()["total"] == 0

        # Four workers, three queued jobs: each job runs exactly once.
        for _ in range(3):
            queued = api.post(f"/projects/{project_id}/arrangements", json={"profile": HANDS},
                              headers=csrf_headers(api))
            assert queued.status_code == 202, queued.text
        ran: list[str] = []
        lock = threading.Lock()
        runner = api.app.state.job_runner

        def handler(ctx):
            with lock:
                ran.append(ctx.job["id"])
            time.sleep(0.05)
            return {}

        runner.handlers = {"arrange": handler}
        workers = [threading.Thread(target=lambda i=i: [runner.run_once(f"pg-w{i}") for _ in range(4)])
                   for i in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert sorted(ran) == sorted(set(ran)) and len(ran) == 3, ran

        archive = api.get("/account/export")
        assert archive.status_code == 200
        names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
        assert any(name.endswith(".mid") for name in names), names

        gone = api.request("DELETE", "/account", json={"password": PASSWORD, "confirm": "DELETE"},
                           headers=csrf_headers(api))
        assert gone.status_code == 200, gone.text

    # Nothing of the user is left: no rows and no stored bytes.
    for table in WORKFLOW_TABLES:
        count = count_rows(table)
        assert count == 0, f"{table} still has {count} row(s) after account deletion"


def check_pool_and_rate_limit_store(test_url: str) -> None:
    database = Database(url=test_url, pool_size=2, pool_timeout=2)
    try:
        store = DatabaseRateLimitStore(database)
        assert [store.hit("pg-key", 3600).count for _ in range(3)] == [1, 2, 3]
        stats = database.stats()
        assert stats.created <= 2 and stats.in_use == 0
        assert store.cleanup() == 0
    finally:
        database.close()


def test_real_postgres_storage_flow() -> None:
    url = postgres_test_url()
    if not url:
        pytest.skip(SKIP_REASON)
    run_postgres_flow(url)


def test_postgres_options_url_uses_percent_encoded_space():
    url = with_query_param(
        "postgresql://postgres:postgres@localhost:5432/arranger_test",
        "options",
        "-c search_path=arranger_test_schema",
    )
    assert "options=-c%20search_path%3Darranger_test_schema" in url


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-rs", "-p", "no:cacheprovider"]))
