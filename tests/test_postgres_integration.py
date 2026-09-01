"""Real Postgres integration test.

Set POSTGRES_TEST_DATABASE_URL to a disposable Postgres database URL before
running this file. The test creates and drops an isolated temporary schema.
"""

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import psycopg
except ImportError:
    psycopg = None

from arranger_api.storage import Storage, connect, init_db  # noqa: E402


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
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


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
            finally:
                conn.close()

        with_env({"ARRANGER_DATABASE_URL": test_url}, check)
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_real_postgres_storage_flow() -> bool:
    url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not url:
        print("  SKIP  test_real_postgres_storage_flow  POSTGRES_TEST_DATABASE_URL is not set")
        return False
    run_postgres_flow(url)
    return True


if __name__ == "__main__":
    failed = 0
    ran = False
    try:
        ran = test_real_postgres_storage_flow()
        if ran:
            print("  PASS  test_real_postgres_storage_flow")
    except AssertionError as exc:
        failed = 1
        print(f"  FAIL  test_real_postgres_storage_flow  {exc}")
    if ran:
        print(f"\n{1 - failed}/1 passed")
    else:
        print("\n0/1 run")
    raise SystemExit(failed)
