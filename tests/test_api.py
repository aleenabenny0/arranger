"""HTTP API tests.

These run when the optional API dependencies are installed:

    pip install -e .[api]
    python tests/test_api.py
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

if TestClient is not None:
    import arranger_api.main as api_main
    from arranger_api.main import app, get_email_sender, get_storage
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


class FakeEmailSender:
    def __init__(self):
        self.sent = []

    def send_password_reset(self, email, reset_link, expires_minutes):
        self.sent.append(
            {
                "email": email,
                "reset_link": reset_link,
                "expires_minutes": expires_minutes,
            }
        )


def client(storage=None, email_sender=None):
    if TestClient is None:
        raise RuntimeError("FastAPI is not installed; install with pip install -e .[api]")
    app.dependency_overrides.clear()
    if storage is not None:
        def override_storage():
            yield storage

        app.dependency_overrides[get_storage] = override_storage
    if email_sender is not None:
        def override_email_sender():
            return email_sender

        app.dependency_overrides[get_email_sender] = override_email_sender
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

    def create_workspace(api):
        headers = csrf_headers(api)
        profile = api.post("/profiles", json=PROFILE, headers=headers).json()["record"]
        score = api.post("/scores", json=SCORE, headers=headers).json()["record"]
        plan = api.post(
            "/plans",
            json={"score_id": score["id"], "plan": PLAN},
            headers=headers,
        ).json()["record"]
        arrangement = api.post(
            "/arrangements/render-and-verify",
            json={
                "score_id": score["id"],
                "profile_id": profile["id"],
                "plan_id": plan["id"],
            },
            headers=headers,
        ).json()["record"]
        run = api.post(
            "/runs/dry-run",
            json={"score_id": score["id"], "profile_id": profile["id"], "max_attempts": 1},
            headers=headers,
        ).json()["record"]
        return {
            "profile": profile,
            "score": score,
            "plan": plan,
            "arrangement": arrangement,
            "run": run,
        }

    def test_health():
        response = client().get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.headers["x-request-id"]


    def test_ready_checks_database():
        conn = connect(":memory:")
        init_db(conn)
        response = client(Storage(conn)).get("/ready")
        assert response.status_code == 200
        assert response.json()["database"] == "ok"


    def test_security_headers_are_set():
        response = client().get("/health")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]

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


    def test_duplicate_registration_returns_clear_error():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))

        register(api, "duplicate@example.com")
        response = api.post(
            "/auth/register",
            json={
                "email": "duplicate@example.com",
                "password": "Password12345",
                "display_name": "Duplicate",
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"]["detail"] == "email is already registered"


    def test_storage_requires_authentication():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))

        response = api.get("/scores")
        assert response.status_code == 401


    def test_large_request_is_rejected():
        api = client()
        response = api.post(
            "/verify",
            content="{}",
            headers={"content-type": "application/json", "content-length": "1000001"},
        )
        assert response.status_code == 413


    def test_list_endpoints_are_paginated():
        conn = connect(":memory:")
        init_db(conn)
        api = client(Storage(conn))
        register(api, "pagination@example.com")
        headers = csrf_headers(api)
        for index in range(3):
            score = dict(SCORE, title=f"score {index}")
            response = api.post("/scores", json=score, headers=headers)
            assert response.status_code == 200

        page = api.get("/scores?limit=2&offset=0")
        assert page.status_code == 200
        assert len(page.json()["records"]) == 2

        next_page = api.get("/scores?limit=2&offset=2")
        assert next_page.status_code == 200
        assert len(next_page.json()["records"]) == 1


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


    def test_users_cannot_access_each_others_saved_resources():
        conn = connect(":memory:")
        init_db(conn)
        storage = Storage(conn)
        owner = client(storage)
        other = client(storage)

        register(owner, "owner-all@example.com")
        records = create_workspace(owner)
        register(other, "other-all@example.com")
        other_headers = csrf_headers(other)

        blocked_reads = [
            other.get(f"/profiles/{records['profile']['id']}"),
            other.get(f"/scores/{records['score']['id']}"),
            other.get(f"/plans/{records['plan']['id']}"),
            other.get(f"/arrangements/{records['arrangement']['id']}"),
            other.get(f"/arrangements/{records['arrangement']['id']}/verdict"),
            other.get(f"/runs/{records['run']['id']}"),
        ]
        assert all(response.status_code == 404 for response in blocked_reads)

        blocked_writes = [
            other.put(f"/profiles/{records['profile']['id']}", json=PROFILE, headers=other_headers),
            other.put(f"/plans/{records['plan']['id']}", json=PLAN, headers=other_headers),
            other.delete(f"/profiles/{records['profile']['id']}", headers=other_headers),
            other.delete(f"/scores/{records['score']['id']}", headers=other_headers),
            other.delete(f"/plans/{records['plan']['id']}", headers=other_headers),
        ]
        assert all(response.status_code == 404 for response in blocked_writes)


    def test_users_cannot_mix_foreign_relationship_ids():
        conn = connect(":memory:")
        init_db(conn)
        storage = Storage(conn)
        owner = client(storage)
        other = client(storage)

        register(owner, "relationship-owner@example.com")
        records = create_workspace(owner)
        register(other, "relationship-other@example.com")
        headers = csrf_headers(other)

        create_plan = other.post(
            "/plans",
            json={"score_id": records["score"]["id"], "plan": PLAN},
            headers=headers,
        )
        assert create_plan.status_code == 404

        create_arrangement = other.post(
            "/arrangements/render-and-verify",
            json={
                "score_id": records["score"]["id"],
                "profile_id": records["profile"]["id"],
                "plan_id": records["plan"]["id"],
            },
            headers=headers,
        )
        assert create_arrangement.status_code == 404

        create_run = other.post(
            "/runs/dry-run",
            json={"score_id": records["score"]["id"], "profile_id": records["profile"]["id"]},
            headers=headers,
        )
        assert create_run.status_code == 404


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
        fake_email = FakeEmailSender()
        api = client(Storage(conn), fake_email)
        register(api, "reset@example.com", "Password12345")

        reset_response = api.post(
            "/auth/password-reset/request",
            json={"email": "reset@example.com"},
        )
        assert reset_response.status_code == 200
        reset_token = reset_response.json()["reset_token"]
        assert reset_response.json()["reset_link"].endswith(f"?reset_token={reset_token}")
        assert fake_email.sent[0]["email"] == "reset@example.com"
        assert fake_email.sent[0]["reset_link"].endswith(f"?reset_token={reset_token}")

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


    def test_production_password_reset_sends_link_without_returning_token():
        conn = connect(":memory:")
        init_db(conn)
        fake_email = FakeEmailSender()
        old_settings = api_main.settings
        api_main.settings = replace(
            old_settings,
            app_env="production",
            app_public_url="https://arranger.example",
            password_reset_minutes=45,
        )
        try:
            api = client(Storage(conn), fake_email)
            register(api, "prod-reset@example.com", "Password12345")

            reset_response = api.post(
                "/auth/password-reset/request",
                json={"email": "prod-reset@example.com"},
            )
            assert reset_response.status_code == 200
            assert reset_response.json() == {"accepted": True}
            assert len(fake_email.sent) == 1
            assert fake_email.sent[0]["email"] == "prod-reset@example.com"
            assert fake_email.sent[0]["expires_minutes"] == 45
            assert fake_email.sent[0]["reset_link"].startswith(
                "https://arranger.example/?reset_token="
            )
        finally:
            api_main.settings = old_settings


    def test_password_reset_unknown_email_does_not_send_email():
        conn = connect(":memory:")
        init_db(conn)
        fake_email = FakeEmailSender()
        api = client(Storage(conn), fake_email)

        reset_response = api.post(
            "/auth/password-reset/request",
            json={"email": "missing@example.com"},
        )

        assert reset_response.status_code == 200
        assert reset_response.json() == {"accepted": True}
        assert fake_email.sent == []


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
