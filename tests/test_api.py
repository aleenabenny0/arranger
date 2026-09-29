"""HTTP API tests: endpoints, ownership, and the saved-work flows.

    pip install -e .[api,dev]
    python -m pytest tests/test_api.py

Hardening-specific behaviour lives next door: `test_api_security.py`
(request guards, headers, errors, metrics), `test_api_auth.py` (passwords,
sessions, CSRF, email verification) and `test_api_lifecycle.py` (app factory,
migrations, connections).
"""

import json
import logging

import pytest

pytest.importorskip("fastapi")

from conftest import (  # noqa: E402
    NEW_PASSWORD,
    PASSWORD,
    PLAN,
    PROFILE,
    SCORE,
    FailingEmailSender,
    FakeEmailSender,
    create_workspace,
    csrf_headers,
    make_client,
    memory_storage,
    production_settings,
    register,
)

client = make_client


def test_health():
    response = client().get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-request-id"]


def test_ready_checks_database():
    response = client(memory_storage()).get("/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert response.headers["cache-control"] == "no-store"


def test_version_endpoint_reports_build_metadata():
    api = client(settings=production_settings(commit_sha="abc123"))
    response = api.get("/version")
    assert response.status_code == 200
    assert response.json()["version"] == "0.1.0"
    assert response.json()["commit_sha"] == "abc123"
    assert response.json()["environment"] == "production"
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")
    assert response.headers["cache-control"] == "no-store"


def test_security_headers_are_set():
    response = client().get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_frontend_is_served_from_root():
    api = client()
    response = api.get("/")
    assert response.status_code == 200
    assert "<title>Arranger</title>" in response.text
    assert 'src="js/app.js"' in response.text
    # The page has no inline script or style attribute for the CSP to refuse.
    assert "<script>" not in response.text and "onclick=" not in response.text
    for path in ("/js/app.js", "/js/view-project.js", "/styles.css", "/privacy.html", "/terms.html",
                 "/cookies.html", "/copyright.html", "/support.html", "/favicon.svg"):
        assert api.get(path).status_code == 200, path


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


def test_deterministic_plan_endpoint_returns_plan_and_metrics():
    response = client().post(
        "/plan/deterministic",
        json={"score": SCORE, "profile": PROFILE},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["plan"]["sections"]
    assert "playable" in body["verdict"]
    assert 0 <= body["fidelity"]["score"] <= 1


def test_plan_analysis_endpoint_returns_musician_and_ml_features():
    response = client().post(
        "/plan/analysis",
        json={"score": SCORE, "profile": PROFILE},
    )
    assert response.status_code == 200
    analysis = response.json()["analysis"]
    assert analysis["regions"]
    assert analysis["note_importance"]
    assert analysis["reduction_priority"]
    assert analysis["retrieved_guidance"]
    assert analysis["candidate_rankings"]
    assert {"pattern", "verifier_cost", "chosen"} <= set(analysis["candidate_rankings"][0])


def test_invalid_plan_returns_400():
    bad_plan = dict(PLAN)
    bad_plan["sections"] = [dict(PLAN["sections"][0], start_bar=2, end_bar=1)]
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
    api = client(memory_storage())
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


def test_persistent_dry_run_saves_candidate_rankings():
    api = client(memory_storage())
    register(api)
    workspace = create_workspace(api)

    response = api.get(
        "/candidate-rankings",
        params={"run_id": workspace["run"]["id"]},
    )
    assert response.status_code == 200
    records = response.json()["records"]
    assert records
    assert any(record["payload"]["chosen"] for record in records)
    assert all(record["run_id"] == workspace["run"]["id"] for record in records)


def test_dry_run_and_rankings_are_one_transaction():
    # Audit: /runs/dry-run created the run, committed, then wrote rankings in a
    # second commit. If the rankings fail, the run must not survive.
    storage = memory_storage()
    api = client(storage, raise_server_exceptions=False)
    register(api)
    headers = csrf_headers(api)
    profile = api.post("/profiles", json=PROFILE, headers=headers).json()["record"]
    score = api.post("/scores", json=SCORE, headers=headers).json()["record"]

    def explode(**kwargs):
        raise RuntimeError("rankings failed")

    storage.create_candidate_rankings = explode
    response = api.post(
        "/runs/dry-run",
        json={"score_id": score["id"], "profile_id": profile["id"], "max_attempts": 1},
        headers=headers,
    )
    assert response.status_code == 500
    assert api.get("/runs").json()["records"] == []


def test_arrangement_rejects_plan_written_for_another_score():
    # Audit 15: plan_record["score_id"] was never compared with request.score_id.
    api = client(memory_storage())
    register(api)
    headers = csrf_headers(api)
    profile = api.post("/profiles", json=PROFILE, headers=headers).json()["record"]
    first = api.post("/scores", json=SCORE, headers=headers).json()["record"]
    second = api.post("/scores", json=dict(SCORE, title="other"), headers=headers).json()["record"]
    plan = api.post(
        "/plans", json={"score_id": first["id"], "plan": PLAN}, headers=headers
    ).json()["record"]

    mismatched = api.post(
        "/arrangements/render-and-verify",
        json={"score_id": second["id"], "profile_id": profile["id"], "plan_id": plan["id"]},
        headers=headers,
    )
    assert mismatched.status_code == 409
    assert mismatched.json()["detail"]["error"] == "plan_score_mismatch"
    assert api.get("/arrangements").json()["records"] == []

    # Near miss: the same plan against its own score still renders.
    matched = api.post(
        "/arrangements/render-and-verify",
        json={"score_id": first["id"], "profile_id": profile["id"], "plan_id": plan["id"]},
        headers=headers,
    )
    assert matched.status_code == 200


def test_feedback_without_a_target_returns_400():
    # Audit 11: domain_error("invalid_input", str(exc), status_code=400) raised
    # TypeError, so this was a 500.
    api = client(memory_storage(), raise_server_exceptions=False)
    register(api)
    response = api.post("/feedback", json={"label": "accepted"}, headers=csrf_headers(api))
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_input"
    assert "candidate_ranking_id or arrangement_id" in response.json()["detail"]["detail"]


def test_feedback_targets_must_share_a_score():
    api = client(memory_storage())
    register(api)
    headers = csrf_headers(api)
    workspace = create_workspace(api)
    rankings = api.get(
        "/candidate-rankings", params={"run_id": workspace["run"]["id"]}
    ).json()["records"]

    other_score = api.post("/scores", json=dict(SCORE, title="other"), headers=headers).json()[
        "record"
    ]
    other_plan = api.post(
        "/plans", json={"score_id": other_score["id"], "plan": PLAN}, headers=headers
    ).json()["record"]
    other_arrangement = api.post(
        "/arrangements/render-and-verify",
        json={
            "score_id": other_score["id"],
            "profile_id": workspace["profile"]["id"],
            "plan_id": other_plan["id"],
        },
        headers=headers,
    ).json()["record"]

    mismatched = api.post(
        "/feedback",
        json={
            "label": "rejected",
            "candidate_ranking_id": rankings[0]["id"],
            "arrangement_id": other_arrangement["id"],
        },
        headers=headers,
    )
    assert mismatched.status_code == 400
    assert "same score" in mismatched.json()["detail"]["detail"]

    # Near misses: both ids on one score, and either id alone, are accepted.
    for body in (
        {
            "label": "accepted",
            "candidate_ranking_id": rankings[0]["id"],
            "arrangement_id": workspace["arrangement"]["id"],
        },
        {"label": "accepted", "candidate_ranking_id": rankings[0]["id"]},
        {"label": "edited", "arrangement_id": other_arrangement["id"], "edited_plan": PLAN},
    ):
        response = api.post("/feedback", json=body, headers=headers)
        assert response.status_code == 200, response.text
    assert len(api.get("/feedback").json()["records"]) == 3


def test_auth_me_and_logout():
    api = client(memory_storage())

    user = register(api, "me@example.com")
    me_response = api.get("/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["user"]["id"] == user["id"]

    logout_response = api.post("/auth/logout", headers=csrf_headers(api))
    assert logout_response.status_code == 200
    assert api.get("/auth/me").status_code == 401


def test_logout_all_revokes_current_user_sessions():
    storage = memory_storage()
    first = client(storage)
    second = client(storage)

    register(first, "logout-all@example.com", PASSWORD)
    login_response = second.post(
        "/auth/login",
        json={"email": "logout-all@example.com", "password": PASSWORD},
    )
    assert login_response.status_code == 200

    response = first.post("/auth/logout-all", headers=csrf_headers(first))

    assert response.status_code == 200
    assert response.json()["revoked_sessions"] >= 2
    assert first.get("/auth/me").status_code == 401
    assert second.get("/auth/me").status_code == 401


def test_duplicate_registration_returns_clear_error():
    api = client(memory_storage())

    register(api, "duplicate@example.com")
    response = api.post(
        "/auth/register",
        json={
            "email": "duplicate@example.com",
            "password": PASSWORD,
            "display_name": "Duplicate",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["detail"] == "email is already registered"


def test_storage_requires_authentication():
    api = client(memory_storage())

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
    api = client(memory_storage())
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
    storage = memory_storage()
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
    storage = memory_storage()
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
    storage = memory_storage()
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
    api = client(memory_storage())
    register(api, "csrf@example.com")

    response = api.post("/scores", json=SCORE)
    assert response.status_code == 403


def test_weak_password_is_rejected():
    api = client(memory_storage())
    response = api.post(
        "/auth/register",
        json={"email": "weak@example.com", "password": "password123", "display_name": ""},
    )
    assert response.status_code == 422 or response.status_code == 400


def test_invalid_email_is_rejected():
    api = client()
    response = api.post(
        "/auth/register",
        json={"email": "not-an-email", "password": PASSWORD, "display_name": ""},
    )
    assert response.status_code == 422


def test_password_reset_flow_revokes_sessions():
    fake_email = FakeEmailSender()
    api = client(memory_storage(), fake_email)
    register(api, "reset@example.com", PASSWORD)

    reset_response = api.post(
        "/auth/password-reset/request",
        json={"email": "reset@example.com"},
    )
    assert reset_response.status_code == 200
    reset_token = reset_response.json()["reset_token"]
    # The token rides in the URL fragment, which browsers never send to a server.
    assert reset_response.json()["reset_link"].endswith(f"/#/reset-password?token={reset_token}")
    assert fake_email.sent[0]["email"] == "reset@example.com"
    assert fake_email.sent[0]["reset_link"].endswith(f"/#/reset-password?token={reset_token}")

    confirm_response = api.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "password": NEW_PASSWORD},
    )
    assert confirm_response.status_code == 200
    assert api.get("/auth/me").status_code == 401

    login_response = api.post(
        "/auth/login",
        json={"email": "reset@example.com", "password": NEW_PASSWORD},
    )
    assert login_response.status_code == 200


def test_production_password_reset_sends_link_without_returning_token():
    fake_email = FakeEmailSender()
    api = client(
        memory_storage(),
        fake_email,
        settings=production_settings(
            public_base_url="https://arranger.example",
            password_reset_minutes=45,
        ),
    )
    register(api, "prod-reset@example.com", PASSWORD)

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
        "https://arranger.example/#/reset-password?token="
    )


def test_password_reset_unknown_email_does_not_send_email():
    fake_email = FakeEmailSender()
    api = client(memory_storage(), fake_email)

    reset_response = api.post(
        "/auth/password-reset/request",
        json={"email": "missing@example.com"},
    )

    assert reset_response.status_code == 200
    assert reset_response.json() == {"accepted": True}
    assert fake_email.sent == []


def test_password_reset_email_failure_logs_status_and_body_without_leaking_secrets():
    # Audit 16: passed when run as a script, failed under pytest, because
    # configure_logging used logging.basicConfig (a no-op once pytest has
    # installed root handlers) and the arranger_api logger stayed at WARNING.
    api = client(memory_storage(), FailingEmailSender())
    register(api, "fail-reset@example.com", PASSWORD)

    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger("arranger_api")
    logger.addHandler(handler)
    try:
        response = api.post(
            "/auth/password-reset/request",
            json={"email": "fail-reset@example.com"},
        )
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 200
    assert response.json()["accepted"] is True

    failure_events = [
        json.loads(message)
        for message in records
        if '"event": "password_reset_email_failed"' in message
    ]
    assert failure_events, "expected a password_reset_email_failed log event"
    event = failure_events[0]
    assert event["status_code"] == 403
    assert event["response_body"] == "error code: 1010"
    assert "reset_link" not in json.dumps(event)
    assert "reset_token" not in json.dumps(event)


def test_email_diagnostics_endpoint_exposes_non_secret_config():
    api = client(settings=production_settings(metrics_token="diagnostics-token-123"))
    response = api.get(
        "/diagnostics/email", headers={"Authorization": "Bearer diagnostics-token-123"}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "provider",
        "has_resend_key",
        "app_public_url",
        "password_reset_from",
    }
    assert isinstance(body["has_resend_key"], bool)
    assert "re_test_key_not_real" not in response.text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
