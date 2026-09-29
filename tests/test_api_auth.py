"""Accounts through the HTTP surface: passwords, sessions, CSRF, email."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from arranger_api.auth import hash_password_pbkdf2, hash_token  # noqa: E402
from arranger_api.main import create_app  # noqa: E402
from arranger_api.storage.repositories import format_timestamp  # noqa: E402
from conftest import (  # noqa: E402
    NEW_PASSWORD,
    ORIGIN,
    PASSWORD,
    PROFILE,
    SCORE,
    FailingEmailSender,
    FakeEmailSender,
    build_settings,
    csrf_headers,
    make_client,
    memory_storage,
    production_settings,
    register,
    token_from_link,
)


def login(api, email, password=PASSWORD):
    return api.post("/auth/login", json={"email": email, "password": password})


# --- 12. password hashing and policy ------------------------------------------


def test_new_passwords_are_stored_as_argon2id():
    storage = memory_storage()
    register(make_client(storage), "argon@example.com")
    stored = storage.get_user_with_password("argon@example.com")["password_hash"]
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_legacy_pbkdf2_hash_still_logs_in_and_is_upgraded():
    # Audit 12: PBKDF2 with no upgrade path.
    storage = memory_storage()
    legacy = hash_password_pbkdf2(PASSWORD, rounds=1_000)
    storage.create_user("legacy@example.com", legacy, "Legacy")
    api = make_client(storage)

    assert login(api, "legacy@example.com", "wrong-" + PASSWORD).status_code == 401
    assert storage.get_user_with_password("legacy@example.com")["password_hash"] == legacy

    assert login(api, "legacy@example.com").status_code == 200
    upgraded = storage.get_user_with_password("legacy@example.com")["password_hash"]
    assert upgraded.startswith("$argon2id$")
    # ...and the upgraded hash keeps working.
    assert login(make_client(storage), "legacy@example.com").status_code == 200


def test_outdated_argon2_parameters_are_rehashed_on_login():
    from arranger_api.auth import PasswordService

    storage = memory_storage()
    weak = PasswordService(time_cost=1, memory_kib=32, parallelism=1).hash(PASSWORD)
    storage.create_user("params@example.com", weak, "Params")
    app = create_app(build_settings(argon2_memory_kib=128, argon2_time_cost=1))

    assert login(make_client(storage, app=app), "params@example.com").status_code == 200
    current = storage.get_user_with_password("params@example.com")["password_hash"]
    assert current != weak and "m=128" in current

    # Near miss: a hash that already matches the parameters is left alone.
    assert login(make_client(storage, app=app), "params@example.com").status_code == 200
    assert storage.get_user_with_password("params@example.com")["password_hash"] == current


def test_unknown_email_costs_a_dummy_verification():
    storage = memory_storage()
    app = create_app(build_settings())
    calls = []
    real = app.state.passwords.verify_dummy
    app.state.passwords.verify_dummy = lambda password: (calls.append(password), real(password))
    api = make_client(storage, app=app)

    unknown = login(api, "ghost@example.com")
    register(make_client(storage, app=app), "real@example.com")
    wrong = login(make_client(storage, app=app), "real@example.com", "wrong-" + PASSWORD)

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert calls == [PASSWORD]


@pytest.mark.parametrize(
    "password",
    [
        "short-9ch",  # 9 characters
        "password123",  # common
        "Password12345",  # common, case-insensitively
        "1q2w3e4r5t6y",  # common keyboard walk
        "aaaaaaaaaaaaaa",  # one repeated character
        "policy@example.com",  # equal to the email
        "x" * 257,  # over 256 bytes
    ],
)
def test_password_policy_rejects(password):
    api = make_client(memory_storage())
    response = api.post(
        "/auth/register",
        json={"email": "policy@example.com", "password": password, "display_name": ""},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "weak_password"
    assert password not in response.text


@pytest.mark.parametrize("password", ["ten-chars!", "x" * 128 + "y" * 128, "correct horse battery"])
def test_password_policy_accepts_reasonable_passwords(password):
    # Near miss: exactly 10 characters, exactly 256 bytes, no digits or capitals.
    api = make_client(memory_storage())
    response = api.post(
        "/auth/register",
        json={"email": "fine@example.com", "password": password, "display_name": ""},
    )
    assert response.status_code == 200


def test_password_change_requires_current_password_and_rotates_sessions():
    storage = memory_storage()
    fake = FakeEmailSender()
    api = make_client(storage, fake)
    register(api, "change@example.com")
    other = make_client(storage, fake)
    assert login(other, "change@example.com").status_code == 200
    old_cookie = api.cookies.get("arranger_session")

    wrong = api.post(
        "/auth/password/change",
        json={"current_password": "not-the-password-1", "new_password": NEW_PASSWORD},
        headers=csrf_headers(api),
    )
    assert wrong.status_code == 403
    weak = api.post(
        "/auth/password/change",
        json={"current_password": PASSWORD, "new_password": "password123"},
        headers=csrf_headers(api),
    )
    assert weak.status_code == 400
    assert other.get("/auth/me").status_code == 200  # nothing revoked by failed attempts

    changed = api.post(
        "/auth/password/change",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=csrf_headers(api),
    )
    assert changed.status_code == 200
    # The caller continues on a new session; the old cookie and the other device are dead.
    assert api.cookies.get("arranger_session") != old_cookie
    assert api.get("/auth/me").status_code == 200
    assert api.post("/scores", json=SCORE, headers=csrf_headers(api)).status_code == 200
    assert other.get("/auth/me").status_code == 401
    assert storage.session_for_token(hash_token(old_cookie)) is None

    assert login(make_client(storage, fake), "change@example.com", PASSWORD).status_code == 401
    assert login(make_client(storage, fake), "change@example.com", NEW_PASSWORD).status_code == 200


def test_password_change_requires_authentication():
    api = make_client(memory_storage())
    response = api.post(
        "/auth/password/change",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 401


# --- 13. CSRF bound to the session, Origin allow-list -------------------------


def test_csrf_token_must_belong_to_the_current_session():
    # Audit 13: any matching cookie/header pair passed; the stored hash was never read.
    storage = memory_storage()
    fake = FakeEmailSender()
    victim = make_client(storage, fake)
    register(victim, "victim@example.com")
    attacker = make_client(storage, fake)
    register(attacker, "attacker@example.com")

    # A pair the attacker planted: cookie and header agree, but not with the session.
    victim.cookies.set("arranger_csrf", "planted-value")
    planted = victim.post("/scores", json=SCORE, headers={"X-CSRF-Token": "planted-value"})
    assert planted.status_code == 403

    # A genuine token, but minted for somebody else's session.
    victim.cookies.delete("arranger_csrf")
    foreign = victim.post("/scores", json=SCORE, headers=csrf_headers(attacker))
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["error"] == "csrf_failed"
    assert victim.get("/scores").json()["records"] == []


def test_own_csrf_token_is_accepted_and_safe_methods_need_none():
    api = make_client(memory_storage())
    register(api, "own-token@example.com")
    assert api.get("/scores").status_code == 200
    assert api.post("/scores", json=SCORE, headers=csrf_headers(api)).status_code == 200
    assert api.put("/profiles/none", json=PROFILE, headers=csrf_headers(api)).status_code == 404


def test_logout_needs_the_session_csrf_token():
    storage = memory_storage()
    api = make_client(storage)
    register(api, "logout-csrf@example.com")
    assert api.post("/auth/logout").status_code == 403
    assert api.post("/auth/logout", headers={"X-CSRF-Token": "guess"}).status_code == 403
    assert api.get("/auth/me").status_code == 200
    assert api.post("/auth/logout", headers=csrf_headers(api)).status_code == 200


UNAUTHENTICATED_WRITES = [
    ("/auth/register", {"email": "o@example.com", "password": PASSWORD, "display_name": ""}),
    ("/auth/login", {"email": "o@example.com", "password": PASSWORD}),
    ("/auth/password-reset/request", {"email": "o@example.com"}),
    ("/auth/password-reset/confirm", {"token": "t" * 32, "password": PASSWORD}),
    ("/auth/email/verify", {"token": "t" * 32}),
]


@pytest.mark.parametrize("path,body", UNAUTHENTICATED_WRITES)
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:8000.evil.example"])
def test_unauthenticated_writes_reject_foreign_origins(path, body, origin):
    api = make_client(memory_storage(), origin=origin)
    response = api.post(path, json=body)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "origin_not_allowed"


def test_foreign_referer_is_rejected_when_origin_is_absent():
    api = make_client(memory_storage(), origin=None)
    body = {"email": "ref@example.com", "password": PASSWORD, "display_name": ""}
    blocked = api.post("/auth/register", json=body, headers={"Referer": "https://evil.example/x"})
    assert blocked.status_code == 403
    allowed = api.post("/auth/register", json=body, headers={"Referer": ORIGIN + "/#/register"})
    assert allowed.status_code == 200


def test_missing_origin_is_allowed_only_without_cookies():
    # A script or CLI: no Origin, no cookies, no ambient authority to abuse.
    api = make_client(memory_storage(), origin=None)
    body = {"email": "cli@example.com", "password": PASSWORD, "display_name": ""}
    assert api.post("/auth/register", json=body).status_code == 200

    # The same client now holds cookies, so it must say where it comes from.
    no_origin = api.post("/scores", json=SCORE, headers=csrf_headers(api))
    assert no_origin.status_code == 403
    assert no_origin.json()["detail"]["error"] == "origin_required"
    with_origin = api.post("/scores", json=SCORE, headers={**csrf_headers(api), "Origin": ORIGIN})
    assert with_origin.status_code == 200


def test_authenticated_writes_reject_foreign_origins_even_with_a_valid_token():
    api = make_client(memory_storage())
    register(api, "origin-auth@example.com")
    response = api.post(
        "/scores", json=SCORE, headers={**csrf_headers(api), "Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert api.get("/scores", headers={"Origin": "https://evil.example"}).status_code == 200


# --- 14. sessions -------------------------------------------------------------


def test_login_revokes_the_session_the_browser_arrived_with():
    # Audit 14: session fixation.
    storage = memory_storage()
    api = make_client(storage)
    register(api, "fixation@example.com")
    before = api.cookies.get("arranger_session")

    assert login(api, "fixation@example.com").status_code == 200
    after = api.cookies.get("arranger_session")

    assert after != before
    assert storage.session_for_token(hash_token(before)) is None
    assert storage.session_for_token(hash_token(after)) is not None


def test_register_revokes_a_presented_session_too():
    storage = memory_storage()
    api = make_client(storage)
    register(api, "first@example.com")
    before = api.cookies.get("arranger_session")
    register(api, "second@example.com")
    assert storage.session_for_token(hash_token(before)) is None
    assert api.get("/auth/me").json()["user"]["email"] == "second@example.com"


def test_idle_sessions_expire_before_their_absolute_expiry():
    storage = memory_storage()
    api = make_client(storage, settings=build_settings(session_idle_days=14, session_days=30))
    register(api, "idle@example.com")
    assert api.get("/auth/me").status_code == 200

    def set_last_seen(days_ago):
        stamp = format_timestamp(datetime.now(timezone.utc) - timedelta(days=days_ago))
        storage.conn.execute("UPDATE sessions SET last_seen_at = ?", (stamp,))
        storage.conn.commit()

    set_last_seen(13)  # near miss: inside the idle window
    assert api.get("/auth/me").status_code == 200
    set_last_seen(15)
    assert api.get("/auth/me").status_code == 401


def test_sessions_can_be_listed_and_revoked_individually():
    storage = memory_storage()
    fake = FakeEmailSender()
    laptop = make_client(storage, fake)
    register(laptop, "devices@example.com")
    phone = make_client(storage, fake)
    phone.headers["User-Agent"] = "PhoneBrowser/1.0"
    assert login(phone, "devices@example.com").status_code == 200

    listing = laptop.get("/auth/sessions")
    assert listing.status_code == 200
    sessions = listing.json()["sessions"]
    assert len(sessions) == 2
    assert [s["current"] for s in sessions].count(True) == 1
    assert {"id", "created_at", "last_seen_at", "user_agent", "ip", "current"} <= set(sessions[0])
    assert "token_hash" not in listing.text and "csrf" not in listing.text
    phone_session = next(s for s in sessions if s["user_agent"] == "PhoneBrowser/1.0")
    assert not phone_session["current"]

    revoked = laptop.delete(f"/auth/sessions/{phone_session['id']}", headers=csrf_headers(laptop))
    assert revoked.status_code == 200
    assert phone.get("/auth/me").status_code == 401
    assert laptop.get("/auth/me").status_code == 200
    assert len(laptop.get("/auth/sessions").json()["sessions"]) == 1


def test_session_ip_is_shown_coarsely():
    storage = memory_storage()
    api = make_client(storage, settings=build_settings(trusted_proxy_count=1))
    api.headers["X-Forwarded-For"] = "203.0.113.77"
    register(api, "coarse@example.com")
    session = api.get("/auth/sessions").json()["sessions"][0]
    assert session["ip"] == "203.0.113.0/24"


def test_users_cannot_revoke_each_others_sessions():
    storage = memory_storage()
    fake = FakeEmailSender()
    owner = make_client(storage, fake)
    register(owner, "session-owner@example.com")
    owner_session = owner.get("/auth/sessions").json()["sessions"][0]["id"]
    other = make_client(storage, fake)
    register(other, "session-other@example.com")

    blocked = other.delete(f"/auth/sessions/{owner_session}", headers=csrf_headers(other))
    assert blocked.status_code == 404
    assert owner.get("/auth/me").status_code == 200


# --- 17. email verification ---------------------------------------------------


def test_registration_sends_a_verification_link_in_the_url_fragment():
    fake = FakeEmailSender()
    api = make_client(memory_storage(), fake)
    user = register(api, "verify@example.com")
    assert user["email_verified"] is False
    assert api.get("/auth/me").json()["user"]["email_verified"] is False

    assert [m["email"] for m in fake.verifications] == ["verify@example.com"]
    link = fake.verifications[0]["verify_link"]
    assert link.startswith("http://127.0.0.1:8000/#/verify-email?token=")
    assert "?" not in link.split("#", 1)[0]  # nothing secret reaches a server or a proxy log


def test_verification_token_is_single_use_and_posted_in_the_body():
    storage = memory_storage()
    fake = FakeEmailSender()
    api = make_client(storage, fake)
    register(api, "once@example.com")
    token = token_from_link(fake.verifications[0]["verify_link"])
    assert token not in str(list(storage.conn.execute("SELECT * FROM email_verification_tokens")))

    assert api.get("/auth/email/verify", params={"token": token}).status_code in (404, 405)
    assert api.get("/auth/me").json()["user"]["email_verified"] is False

    assert api.post("/auth/email/verify", json={"token": "x" * 40}).status_code == 403
    verified = api.post("/auth/email/verify", json={"token": token})
    assert verified.status_code == 200
    assert verified.json()["user"]["email_verified"] is True
    assert api.get("/auth/me").json()["user"]["email_verified"] is True
    assert api.post("/auth/email/verify", json={"token": token}).status_code == 403


def test_expired_verification_token_is_rejected():
    storage = memory_storage()
    fake = FakeEmailSender()
    api = make_client(storage, fake)
    register(api, "late@example.com")
    token = token_from_link(fake.verifications[0]["verify_link"])
    past = format_timestamp(datetime.now(timezone.utc) - timedelta(minutes=1))
    storage.conn.execute("UPDATE email_verification_tokens SET expires_at = ?", (past,))
    storage.conn.commit()
    assert api.post("/auth/email/verify", json={"token": token}).status_code == 403


def test_verification_email_can_be_resent_with_a_rate_limit():
    fake = FakeEmailSender()
    settings = build_settings(email_resend_rate_limit_requests=2)
    api = make_client(memory_storage(), fake, settings=settings)
    assert api.post("/auth/email/resend").status_code in (401, 403)
    register(api, "resend@example.com")

    statuses = [
        api.post("/auth/email/resend", headers=csrf_headers(api)).status_code for _ in range(3)
    ]
    assert statuses == [200, 200, 429]
    assert len(fake.verifications) == 3  # one from registration, two resent

    # The newest link works; verifying retires the older ones.
    newest = token_from_link(fake.verifications[-1]["verify_link"])
    oldest = token_from_link(fake.verifications[0]["verify_link"])
    assert api.post("/auth/email/verify", json={"token": newest}).status_code == 200
    assert api.post("/auth/email/verify", json={"token": oldest}).status_code == 403


def test_verified_email_can_be_required_for_expensive_operations():
    storage = memory_storage()
    fake = FakeEmailSender()
    settings = production_settings(require_verified_email=True)
    api = make_client(storage, fake, settings=settings)
    register(api, "gate@example.com")
    headers = csrf_headers(api)
    profile = api.post("/profiles", json=PROFILE, headers=headers).json()["record"]
    score = api.post("/scores", json=SCORE, headers=headers).json()["record"]
    run_body = {"score_id": score["id"], "profile_id": profile["id"], "max_attempts": 1}

    blocked = api.post("/runs/dry-run", json=run_body, headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "email_not_verified"

    token = token_from_link(fake.verifications[0]["verify_link"])
    assert api.post("/auth/email/verify", json={"token": token}).status_code == 200
    assert api.post("/runs/dry-run", json=run_body, headers=headers).status_code == 200


def test_verified_email_requirement_defaults():
    assert build_settings().require_verified_email is False
    # Near miss: with the requirement off, an unverified user is not blocked.
    api = make_client(memory_storage())
    register(api, "nogate@example.com")
    headers = csrf_headers(api)
    profile = api.post("/profiles", json=PROFILE, headers=headers).json()["record"]
    score = api.post("/scores", json=SCORE, headers=headers).json()["record"]
    run = api.post(
        "/runs/dry-run",
        json={"score_id": score["id"], "profile_id": profile["id"], "max_attempts": 1},
        headers=headers,
    )
    assert run.status_code == 200


def test_completing_a_password_reset_verifies_the_address():
    fake = FakeEmailSender()
    api = make_client(memory_storage(), fake)
    register(api, "reset-verifies@example.com")
    api.post("/auth/password-reset/request", json={"email": "reset-verifies@example.com"})
    token = token_from_link(fake.sent[0]["reset_link"])
    confirmed = api.post("/auth/password-reset/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert confirmed.json()["user"]["email_verified"] is True


# --- 8. email delivery failures -----------------------------------------------


def test_production_delivery_failure_is_logged_counted_and_surfaced_on_ready(caplog, tmp_path):
    # Audit 8: production answered {"accepted": true} and nothing else noticed.
    storage = memory_storage()
    # /ready also checks the file store. This test swaps in an in-memory database without
    # running startup, so files go to a directory instead of to tables that were never made.
    settings = production_settings(metrics_token="metrics-secret-token", artifact_backend="local",
                                   artifact_dir=str(tmp_path / "files"))
    app = create_app(settings)
    good = make_client(storage, FakeEmailSender(), app=app)
    register(good, "delivery@example.com")
    assert good.get("/ready").json()["email"] == "ok"

    failing = make_client(storage, FailingEmailSender(), app=app)
    with caplog.at_level(logging.INFO, logger="arranger_api"):
        known = failing.post("/auth/password-reset/request", json={"email": "delivery@example.com"})
        unknown = failing.post("/auth/password-reset/request", json={"email": "nobody@example.com"})

    # Identical public answers: no account enumeration, no hint of the failure.
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json() == {"accepted": True}

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("password_reset_email_failed" in r.getMessage() for r in errors)
    assert not any("token=" in r.getMessage() for r in caplog.records)

    assert failing.get("/ready").json()["email"] == "degraded"
    text = failing.get("/metrics", headers={"Authorization": "Bearer metrics-secret-token"}).text
    assert 'arranger_email_send_total{kind="password_reset",outcome="failed"} 1' in text
    assert 'arranger_email_send_total{kind="email_verification",outcome="sent"} 1' in text

    # A later success clears the degraded state.
    healthy = make_client(storage, FakeEmailSender(), app=app)
    healthy.post("/auth/password-reset/request", json={"email": "delivery@example.com"})
    assert healthy.get("/ready").json()["email"] == "ok"


def test_reset_response_never_includes_the_token_outside_local_environments():
    api = make_client(memory_storage(), settings=production_settings())
    register(api, "no-token@example.com")
    response = api.post("/auth/password-reset/request", json={"email": "no-token@example.com"})
    assert response.json() == {"accepted": True}


def test_ready_reports_migration_version():
    body = make_client(memory_storage()).get("/ready").json()
    assert body["migrations"]["pending"] == 0
    from arranger_api.storage.migrations import MIGRATIONS

    assert body["migrations"]["current"] == MIGRATIONS[-1].id
    assert body["email"] == "ok"
