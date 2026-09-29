"""Shared test configuration and API test helpers.

The environment is fixed here, before anything imports `arranger_api.main`
(which builds the default app from the environment at import time):

- the default app's database is a throwaway SQLite file, never `data/arranger.db`
  and never a developer's `DATABASE_URL`;
- Argon2 runs with toy parameters so hashing does not dominate the suite;
- no test ever sends real email: every client gets a `FakeEmailSender` unless a
  test supplies its own fake.

Plain helpers (not fixtures) live here too, so `python tests/test_api.py` keeps
working as a direct runner alongside pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_TMP = tempfile.mkdtemp(prefix="arranger-tests-")
for _name in ("DATABASE_URL", "ARRANGER_DATABASE_URL", "METRICS_TOKEN", "FRONTEND_ORIGINS"):
    os.environ.pop(_name, None)
os.environ["APP_ENV"] = "test"
os.environ["ARRANGER_DB_PATH"] = str(Path(_TMP) / "default-app.db")
os.environ["EMAIL_PROVIDER"] = "console"
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_MEMORY_KIB", "64")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

ORIGIN = "http://127.0.0.1:8000"
PASSWORD = "correct-horse-battery-7"
NEW_PASSWORD = "another-valid-passphrase-9"

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
    """Records messages instead of sending them."""

    def __init__(self):
        self.sent = []
        self.verifications = []

    def send_password_reset(self, email, reset_link, expires_minutes):
        self.sent.append(
            {"email": email, "reset_link": reset_link, "expires_minutes": expires_minutes}
        )

    def send_email_verification(self, email, verify_link, expires_minutes):
        self.verifications.append(
            {"email": email, "verify_link": verify_link, "expires_minutes": expires_minutes}
        )


class FailingEmailSender:
    """Every send fails the way a provider rejection does."""

    def __init__(self, status_code=403, body="error code: 1010"):
        self.status_code = status_code
        self.body = body

    def _fail(self):
        from arranger_api.email import EmailSendError

        raise EmailSendError(
            f"Resend returned status {self.status_code}",
            status_code=self.status_code,
            body=self.body,
        )

    def send_password_reset(self, email, reset_link, expires_minutes):
        self._fail()

    def send_email_verification(self, email, verify_link, expires_minutes):
        self._fail()


def token_from_link(link: str) -> str:
    from urllib.parse import unquote

    return unquote(link.rsplit("token=", 1)[1])


def memory_storage():
    from arranger_api.storage import Storage, connect, init_db

    conn = connect(":memory:")
    init_db(conn)
    return Storage(conn)


def build_settings(**overrides):
    """Settings for a throwaway app. Defaults to APP_ENV=test."""
    from arranger_api.settings import load_settings

    return replace(load_settings(), **overrides)


def production_settings(**overrides):
    """A production configuration that passes startup validation."""
    values = {
        "app_env": "production",
        "public_base_url": "https://arranger.example",
        "cors_origins": ["https://arranger.example"],
        "cookie_secure": True,
        "csrf_protection": True,
        "email_provider": "resend",
        "resend_api_key": "re_test_key_not_real",
        "database_url": "",
        "allow_sqlite_in_production": True,
        "rate_limit_backend": "memory",
        "require_verified_email": True,
        "reload": False,
        "artifact_backend": "database",
        "job_workers": 0,
    }
    values.update(overrides)
    return build_settings(**values)


def make_client(
    storage=None,
    email_sender=None,
    *,
    settings=None,
    origin: str | None = "default",
    app=None,
    raise_server_exceptions: bool = True,
):
    """A TestClient over the default app, or over a fresh app when `settings` is given.

    `origin` becomes the default `Origin` header, as a browser on the frontend
    would send; by default it is the app's first allowed origin. Pass
    `origin=None` to act as a non-browser client.
    """
    from fastapi.testclient import TestClient

    from arranger_api.main import app as default_app
    from arranger_api.main import create_app, get_email_sender, get_storage

    if app is None:
        app = create_app(settings) if settings is not None else default_app
    app.dependency_overrides.clear()
    # The default app is shared by the whole suite; its per-process rate-limit
    # counters must not leak from one test into the next.
    clear = getattr(app.state.rate_limits.store, "clear", None)
    if clear is not None:
        clear()
    if storage is not None:

        def override_storage():
            yield storage

        app.dependency_overrides[get_storage] = override_storage
    sender = email_sender if email_sender is not None else FakeEmailSender()
    app.dependency_overrides[get_email_sender] = lambda: sender
    if origin == "default":
        origin = app.state.settings.cors_origins[0]
    headers = {"Origin": origin} if origin else {}
    # Secure cookies are only returned over https, as in a browser.
    scheme = "https" if app.state.settings.cookie_secure else "http"
    client = TestClient(
        app,
        base_url=f"{scheme}://testserver",
        headers=headers,
        raise_server_exceptions=raise_server_exceptions,
    )
    client.email_sender = sender
    return client


def csrf_headers(api):
    return {"X-CSRF-Token": api.cookies.get("arranger_csrf")}


def register(api, email="user@example.com", password=PASSWORD):
    response = api.post(
        "/auth/register",
        json={"email": email, "password": password, "display_name": "Test User"},
    )
    assert response.status_code == 200, response.text
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
        json={"score_id": score["id"], "profile_id": profile["id"], "plan_id": plan["id"]},
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
