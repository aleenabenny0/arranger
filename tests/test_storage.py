"""Storage repository tests."""

import sys
from pathlib import Path

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


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}  {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
