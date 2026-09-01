"""HTTP API tests.

These run when the optional API dependencies are installed:

    pip install -e .[api]
    python tests/test_api.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

if TestClient is not None:
    from arranger_api.main import app, get_storage
    from arranger_api.storage import Storage, connect, init_db


SCORE = {
    "title": "api fixture",
    "tempo_bpm": 100,
    "notes": [
        {"pitch": 60, "onset": 0.0, "duration": 0.5, "bar": 1},
        {"pitch": 64, "onset": 0.0, "duration": 0.5, "bar": 1},
        {"pitch": 67, "onset": 0.0, "duration": 0.5, "bar": 1},
        {"pitch": 72, "onset": 0.0, "duration": 0.5, "bar": 1},
    ],
}

PROFILE = {
    "name": "api",
    "instrument": "piano",
    "lowest_pitch": 21,
    "highest_pitch": 108,
    "max_span": 12,
    "comfortable_span": 9,
    "max_notes_per_hand": 5,
    "max_leap_rate": 90,
    "leap_slack": 5,
    "skill_level": 4,
}

PLAN = {
    "title": "api plan",
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
            "label": "one bar",
        }
    ],
    "reductions": [],
    "pedal_bars": [],
    "notes": "test plan",
}


def client(storage=None):
    if TestClient is None:
        raise RuntimeError("FastAPI is not installed; install with pip install -e .[api]")
    app.dependency_overrides.clear()
    if storage is not None:
        def override_storage():
            yield storage

        app.dependency_overrides[get_storage] = override_storage
    return TestClient(app)


if TestClient is not None:

    def csrf_headers(api):
        return {"X-CSRF-Token": api.cookies.get("arranger_csrf")}

    def register(api, email="user@example.com", password="Password12345"):
        response = api.post(
            "/auth/register",
            json={
                "email": email,
                "password": password,
                "display_name": "Test User",
            },
        )
        assert response.status_code == 200
        return response.json()["user"]

    def test_health():
        response = client().get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_frontend_is_served_from_root():
        response = client().get("/")
        assert response.status_code == 200
        assert "Arranger Workspace" in response.text


    def test_verify_endpoint():
        response = client().post("/verify", json={"score": SCORE, "profile": PROFILE})
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "api fixture"
        assert "playable" in body
        assert "violations" in body


    def test_render_and_verify_endpoint():
        response = client().post(
            "/render-and-verify",
            json={"source": SCORE, "profile": PROFILE, "plan": PLAN},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["arranged"]["notes"]
        assert "verdict" in body
        assert 0 <= body["fidelity"]["score"] <= 1


    def test_invalid_plan_returns_400():
        bad_plan = dict(PLAN)
        bad_plan["sections"] = [
            dict(PLAN["sections"][0], start_bar=2, end_bar=1)
        ]
        response = client().post(
            "/render-and-verify",
            json={"source": SCORE, "profile": PROFILE, "plan": bad_plan},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "invalid_input"


    def test_unknown_fields_are_rejected():
        bad_score = dict(SCORE)
        bad_score["surprise"] = True
        response = client().post("/verify", json={"score": bad_score, "profile": PROFILE})
        assert response.status_code == 422


    def test_persistent_render_and_verify_flow():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))
        register(api)

        headers = csrf_headers(api)

        profile_response = api.post("/profiles", json=PROFILE, headers=headers)
        assert profile_response.status_code == 200
        profile_id = profile_response.json()["record"]["id"]

        score_response = api.post("/scores", json=SCORE, headers=headers)
        assert score_response.status_code == 200
        score_id = score_response.json()["record"]["id"]

        plan_response = api.post(
            "/plans",
            json={"score_id": score_id, "plan": PLAN},
            headers=headers,
        )
        assert plan_response.status_code == 200
        plan_id = plan_response.json()["record"]["id"]

        arrangement_response = api.post(
            "/arrangements/render-and-verify",
            json={"score_id": score_id, "profile_id": profile_id, "plan_id": plan_id},
            headers=headers,
        )
        assert arrangement_response.status_code == 200
        arrangement = arrangement_response.json()["record"]
        assert arrangement["arranged_score"]["notes"]
        assert "score" in arrangement["fidelity"]

        verdict_response = api.get(f"/arrangements/{arrangement['id']}/verdict")
        assert verdict_response.status_code == 200
        assert "playable" in verdict_response.json()

        run_response = api.post(
            "/runs/dry-run",
            json={"score_id": score_id, "profile_id": profile_id, "max_attempts": 1},
            headers=headers,
        )
        assert run_response.status_code == 200
        assert run_response.json()["record"]["payload"]["title"] == "api fixture"


    def test_auth_me_and_logout():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))

        user = register(api, "me@example.com")
        me_response = api.get("/auth/me")
        assert me_response.status_code == 200
        assert me_response.json()["user"]["id"] == user["id"]

        logout_response = api.post("/auth/logout", headers=csrf_headers(api))
        assert logout_response.status_code == 200
        assert api.get("/auth/me").status_code == 401


    def test_storage_requires_authentication():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))

        response = api.get("/scores")
        assert response.status_code == 401


    def test_users_cannot_access_each_others_scores():
        conn = connect(":memory:")
        init_db(conn)
        storage = Storage(conn)
        owner = client(storage)
        other = client(storage)

        register(owner, "owner@example.com")
        score_response = owner.post("/scores", json=SCORE, headers=csrf_headers(owner))
        assert score_response.status_code == 200
        score_id = score_response.json()["record"]["id"]

        register(other, "other@example.com")
        blocked = other.get(f"/scores/{score_id}")
        assert blocked.status_code == 404


    def test_protected_write_requires_csrf_token():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))
        register(api, "csrf@example.com")

        response = api.post("/scores", json=SCORE)
        assert response.status_code == 403


    def test_weak_password_is_rejected():
        api = client()
        response = api.post(
            "/auth/register",
            json={"email": "weak@example.com", "password": "password123", "display_name": ""},
        )
        assert response.status_code == 422 or response.status_code == 400


    def test_password_reset_flow_revokes_sessions():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))
        register(api, "reset@example.com", "Password12345")

        reset_response = api.post(
            "/auth/password-reset/request",
            json={"email": "reset@example.com"},
        )
        assert reset_response.status_code == 200
        reset_token = reset_response.json()["reset_token"]

        confirm_response = api.post(
            "/auth/password-reset/confirm",
            json={"token": reset_token, "password": "NewPassword12345"},
        )
        assert confirm_response.status_code == 200
        assert api.get("/auth/me").status_code == 401

        login_response = api.post(
            "/auth/login",
            json={"email": "reset@example.com", "password": "NewPassword12345"},
        )
        assert login_response.status_code == 200


if __name__ == "__main__":
    if TestClient is None:
        print("SKIP  FastAPI is not installed; run: pip install -e .[api]")
        raise SystemExit(0)

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
