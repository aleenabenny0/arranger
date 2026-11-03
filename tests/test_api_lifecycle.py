"""Application factory, startup migrations, connections and housekeeping."""

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, Depends, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import arranger_api.main as api_main  # noqa: E402
from arranger_api.main import create_app, get_storage, run_cleanup  # noqa: E402
from arranger_api.settings import ConfigurationError  # noqa: E402
from arranger_api.storage import Storage, connect  # noqa: E402
from arranger_api.storage.repositories import format_timestamp  # noqa: E402
from conftest import (  # noqa: E402
    ORIGIN,
    PASSWORD,
    SCORE,
    build_settings,
    csrf_headers,
    make_client,
    production_settings,
    register,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def file_settings(tmp_path, **overrides):
    overrides.setdefault("sqlite_path", str(tmp_path / "app.db"))
    overrides.setdefault("cleanup_interval_seconds", 3600)
    return build_settings(**overrides)


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# --- structural 1: app factory ------------------------------------------------


def test_create_app_builds_independent_apps_with_their_own_settings():
    first = create_app(build_settings(commit_sha="one"))
    second = create_app(build_settings(commit_sha="two"))
    assert first is not second
    assert TestClient(first).get("/version").json()["commit_sha"] == "one"
    assert TestClient(second).get("/version").json()["commit_sha"] == "two"
    assert first.state.metrics is not second.state.metrics


def test_module_level_app_and_dependencies_stay_importable():
    from arranger_api.main import app, get_email_sender, get_storage  # noqa: F401

    assert app is api_main.app
    assert app.state.settings.app_env == "test"


def test_routers_reach_settings_and_shared_services_through_app_state(tmp_path):
    app = create_app(file_settings(tmp_path, frontend_dir=Path("__none__"), commit_sha="state"))
    router = APIRouter()

    @router.get("/probe/state")
    def probe(request: Request, storage: Storage = Depends(get_storage)) -> dict:
        state = request.app.state
        return {
            "commit": state.settings.commit_sha,
            "limiter": state.rate_limits.limiter("probe", 5, 60).name,
            "dialect": storage.dialect,
        }

    app.include_router(router)
    with TestClient(app) as api:
        assert api.get("/probe/state").json() == {
            "commit": "state",
            "limiter": "probe",
            "dialect": "sqlite",
        }


def test_feature_routers_are_registered_before_the_static_mount(monkeypatch):
    import arranger_api.routers as routers

    calls = []

    def fake_register(app):
        calls.append([getattr(route, "name", None) for route in app.router.routes])

    monkeypatch.setattr(routers, "register_routers", fake_register)
    create_app(build_settings())
    assert len(calls) == 1
    assert "frontend" not in calls[0]


# --- structural 2: migrations out of the request path -------------------------


def test_startup_runs_migrations_once_and_requests_never_do(tmp_path, monkeypatch):
    settings = file_settings(tmp_path)
    app = create_app(settings)
    assert not Path(settings.sqlite_path).exists()  # nothing is opened until startup

    runs = []
    import arranger_api.storage.database as database_module

    real = database_module.run_migrations
    monkeypatch.setattr(
        database_module, "run_migrations", lambda conn: (runs.append(1), real(conn))[1]
    )
    with make_client(app=app) as api:
        assert len(runs) == 1
        assert "schema_migrations" in tables(settings.sqlite_path)
        register(api, "lifespan@example.com")
        for _ in range(5):
            assert api.get("/scores").status_code == 200
        assert api.get("/ready").json()["migrations"]["pending"] == 0
    assert len(runs) == 1


def test_migrations_can_be_left_to_a_release_step(tmp_path):
    settings = file_settings(tmp_path, run_migrations_on_startup=False)
    with make_client(app=create_app(settings), raise_server_exceptions=False) as api:
        if Path(settings.sqlite_path).exists():
            assert "users" not in tables(settings.sqlite_path)
        not_ready = api.get("/ready")
        assert not_ready.status_code == 503
        assert not_ready.json()["detail"]["error"] == "migrations_pending"
        assert set(not_ready.json()["detail"]) == {"error", "detail", "request_id"}

        result = subprocess.run(
            [sys.executable, "-m", "arranger_api.storage.migrate"],
            cwd=REPO_ROOT,
            env={**_clean_env(), "ARRANGER_DB_PATH": settings.sqlite_path},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "applied: 0001_initial_storage" in result.stdout
        assert api.get("/ready").status_code == 200


def test_run_migrations_on_startup_defaults_to_true():
    assert build_settings().run_migrations_on_startup is True


def _clean_env():
    import os

    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "ARRANGER_DATABASE_URL"}}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return env


def test_migrate_cli_check_mode_reports_pending_without_changing_anything(tmp_path):
    db = str(tmp_path / "cli.db")
    env = {**_clean_env(), "ARRANGER_DB_PATH": db}
    command = [sys.executable, "-m", "arranger_api.storage.migrate"]

    check = subprocess.run([*command, "--check"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert check.returncode == 1
    assert "pending: 0001_initial_storage" in check.stdout
    assert "users" not in tables(db)

    assert subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, timeout=60).returncode == 0
    again = subprocess.run([*command, "--check"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert again.returncode == 0
    assert "pending" not in again.stdout


def test_dead_pre_migration_schema_copies_are_gone():
    import arranger_api.storage.database as database_module

    assert not hasattr(database_module, "init_sqlite")
    assert not hasattr(database_module, "init_postgres")
    assert callable(database_module.init_db)


# --- structural 3: connections and transactions -------------------------------


def test_failed_request_rolls_back_before_the_connection_is_released(tmp_path):
    settings = file_settings(tmp_path, frontend_dir=Path("__none__"))
    app = create_app(settings)
    router = APIRouter()
    seen = {}

    @router.post("/probe/half-write")
    def half_write(storage: Storage = Depends(get_storage)) -> dict:
        storage.conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name, created_at, updated_at)"
            " VALUES ('u1', 'ghost@example.com', 'x', 'Ghost', 'now', 'now')"
        )
        seen["conn"] = storage.conn
        raise RuntimeError("fails after writing, before committing")

    app.include_router(router)
    with make_client(app=app, raise_server_exceptions=False) as api:
        assert api.post("/probe/half-write").status_code == 500
        check = connect(settings.sqlite_path)
        try:
            assert check.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0
        finally:
            check.close()
        # SQLite connections are per request: this one was closed on release.
        with pytest.raises(sqlite3.ProgrammingError):
            seen["conn"].execute("SELECT 1")
        # and the next request is unaffected
        register(api, "after-failure@example.com")


def test_sqlite_connections_use_wal_foreign_keys_and_busy_timeout(tmp_path):
    from arranger_api.storage import Database

    database = Database(sqlite_path=tmp_path / "pragmas.db", busy_timeout_ms=4321)
    with database.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321


def test_database_outage_is_a_generic_503(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file, so the database directory cannot be created")
    settings = file_settings(
        tmp_path,
        sqlite_path=str(blocker / "sub" / "app.db"),
        run_migrations_on_startup=False,
    )
    api = make_client(app=create_app(settings), raise_server_exceptions=False)
    response = api.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "database_unavailable"
    assert "not-a-directory" not in response.text


def test_last_seen_is_not_rewritten_on_every_request(tmp_path):
    settings = file_settings(tmp_path)
    with make_client(app=create_app(settings)) as api:
        register(api, "touch@example.com")
        conn = connect(settings.sqlite_path)
        try:
            def last_seen():
                return conn.execute("SELECT last_seen_at FROM sessions").fetchone()["last_seen_at"]

            first = last_seen()
            for _ in range(5):
                assert api.get("/auth/me").status_code == 200
            assert last_seen() == first

            # Once the stamp is older than five minutes it is refreshed, once.
            stale = format_timestamp(datetime.now(timezone.utc) - timedelta(minutes=6))
            conn.execute("UPDATE sessions SET last_seen_at = ?", (stale,))
            conn.commit()
            assert api.get("/auth/me").status_code == 200
            refreshed = last_seen()
            assert refreshed > stale
            assert api.get("/auth/me").status_code == 200
            assert last_seen() == refreshed
        finally:
            conn.close()


# --- 14: periodic cleanup -----------------------------------------------------


def test_cleanup_deletes_dead_sessions_and_spent_tokens(tmp_path):
    settings = file_settings(tmp_path)
    app = create_app(settings)
    with make_client(app=app) as api:
        register(api, "cleanup@example.com")
        api.post("/auth/password-reset/request", json={"email": "cleanup@example.com"})
        live_cookie = api.cookies.get("arranger_session")

        conn = connect(settings.sqlite_path)
        storage = Storage(conn)
        try:
            user_id = storage.get_user_with_password("cleanup@example.com")["id"]
            storage.create_session(user_id, "revoked-token")
            storage.revoke_session("revoked-token")
            storage.create_session(user_id, "expired-token")
            storage.create_session(user_id, "idle-token")
            past = format_timestamp(datetime.now(timezone.utc) - timedelta(days=1))
            long_ago = format_timestamp(datetime.now(timezone.utc) - timedelta(days=20))
            conn.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = 'expired-token'", (past,))
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = 'idle-token'", (long_ago,))
            conn.execute("UPDATE password_reset_tokens SET expires_at = ?", (past,))
            conn.execute("UPDATE email_verification_tokens SET used_at = ?", (past,))
            conn.commit()

            removed = run_cleanup(app)

            assert removed["sessions"] == 3
            assert removed["password_reset_tokens"] == 1
            assert removed["email_verification_tokens"] == 1
            remaining = [row["token_hash"] for row in conn.execute("SELECT token_hash FROM sessions")]
            assert len(remaining) == 1 and remaining[0] not in {"revoked-token", "expired-token", "idle-token"}
        finally:
            conn.close()
        # Near miss: the live session survived the sweep.
        assert api.cookies.get("arranger_session") == live_cookie
        assert api.get("/auth/me").status_code == 200


# --- 4: database-backed rate limiting through the app -------------------------


def test_database_rate_limit_is_shared_between_app_instances(tmp_path):
    settings = file_settings(tmp_path, rate_limit_backend="database", read_rate_limit_requests=3)
    first_app, second_app = create_app(settings), create_app(settings)
    with make_client(app=first_app) as first, make_client(app=second_app) as second:
        statuses = [
            first.get("/version").status_code,
            second.get("/version").status_code,
            first.get("/version").status_code,
            second.get("/version").status_code,
        ]
        assert statuses == [200, 200, 200, 429]
        limited = first.get("/version")
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1


def test_rate_limit_backend_defaults():
    assert build_settings().rate_limit_backend == "memory"
    assert create_app(build_settings()).state.rate_limits.store.__class__.__name__ == (
        "InMemoryRateLimitStore"
    )


def test_database_rate_limit_outage_falls_back_instead_of_failing_requests(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    settings = file_settings(
        tmp_path,
        sqlite_path=str(blocker / "app.db"),
        rate_limit_backend="database",
        read_rate_limit_requests=2,
        run_migrations_on_startup=False,
        metrics_token="metrics-secret-token",
    )
    api = make_client(app=create_app(settings), raise_server_exceptions=False)
    # Still served, and still limited (per process) while the store is down.
    assert [api.get("/version").status_code for _ in range(3)] == [200, 200, 429]
    text = api.get("/metrics", headers={"Authorization": "Bearer metrics-secret-token"}).text
    assert "arranger_rate_limit_store_errors_total 0" not in text


# --- 18 / 8: fail fast at startup ---------------------------------------------


def test_create_app_refuses_unsafe_production_configuration():
    with pytest.raises(ConfigurationError) as excinfo:
        create_app(production_settings(cookie_secure=False, cors_origins=["*"]))
    message = str(excinfo.value)
    assert "COOKIE_SECURE" in message and "FRONTEND_ORIGINS" in message


def test_production_never_falls_back_to_the_console_email_sender():
    # Audit 8: any configuration error silently became the console provider.
    with pytest.raises(ConfigurationError, match="RESEND_API_KEY"):
        create_app(production_settings(resend_api_key=""))
    with pytest.raises(ConfigurationError, match="EMAIL_PROVIDER"):
        create_app(production_settings(email_provider="console"))
    with pytest.raises(ConfigurationError, match="not supported"):
        create_app(build_settings(email_provider="carrier-pigeon"))

    app = create_app(production_settings())
    assert app.state.email_sender.__class__.__name__ == "ResendEmailSender"


def test_end_to_end_on_a_real_file_database(tmp_path):
    # The whole stack with nothing overridden except the email sender.
    settings = file_settings(tmp_path)
    with make_client(app=create_app(settings)) as api:
        register(api, "e2e@example.com", PASSWORD)
        created = api.post("/scores", json=SCORE, headers=csrf_headers(api))
        assert created.status_code == 200
        assert api.get("/scores").json()["records"][0]["id"] == created.json()["record"]["id"]
        assert api.headers["Origin"] == ORIGIN
