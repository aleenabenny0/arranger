"""Environment settings tests."""

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger_api.settings import (  # noqa: E402
    ConfigurationError,
    configuration_problems,
    load_settings,
    validate_settings,
)


def with_env(values, fn):
    old = os.environ.copy()
    os.environ.update(values)
    try:
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_port_and_cookie_secure_come_from_environment():
    def check():
        settings = load_settings()
        assert settings.port == 9999
        assert settings.cookie_secure
        assert not settings.reload
        assert settings.log_level == "DEBUG"

    with_env(
        {
            "APP_ENV": "production",
            "PORT": "9999",
            "COOKIE_SECURE": "true",
            "RELOAD": "false",
            "LOG_LEVEL": "DEBUG",
        },
        check,
    )


def test_frontend_origins_are_comma_separated():
    def check():
        settings = load_settings()
        assert settings.cors_origins == ["https://example.com", "https://app.example.com"]

    with_env(
        {"FRONTEND_ORIGINS": "https://example.com, https://app.example.com"},
        check,
    )


def test_frontend_dir_can_come_from_environment():
    def check():
        settings = load_settings()
        assert str(settings.frontend_dir) == "custom-frontend"

    with_env({"FRONTEND_DIR": "custom-frontend"}, check)


def test_security_settings_come_from_environment():
    def check():
        settings = load_settings()
        assert settings.max_sessions_per_user == 2
        assert not settings.csrf_protection
        assert settings.rate_limit_requests == 10
        assert settings.rate_limit_window_seconds == 5
        assert settings.auth_rate_limit_requests == 7
        assert settings.auth_rate_limit_window_seconds == 30
        assert settings.password_reset_rate_limit_requests == 3
        assert settings.password_reset_rate_limit_window_seconds == 90
        assert settings.max_request_bytes == 2048
        assert settings.password_reset_minutes == 15
        assert settings.app_public_url == "https://arranger.example"
        assert settings.email_provider == "resend"
        assert settings.resend_api_key == "test-key"
        assert settings.password_reset_from == "Arranger <support@arranger.example>"
        assert settings.password_reset_subject == "Reset access"

    with_env(
        {
            "MAX_SESSIONS_PER_USER": "2",
            "CSRF_PROTECTION": "false",
            "RATE_LIMIT_REQUESTS": "10",
            "RATE_LIMIT_WINDOW_SECONDS": "5",
            "AUTH_RATE_LIMIT_REQUESTS": "7",
            "AUTH_RATE_LIMIT_WINDOW_SECONDS": "30",
            "PASSWORD_RESET_RATE_LIMIT_REQUESTS": "3",
            "PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS": "90",
            "MAX_REQUEST_BYTES": "2048",
            "PASSWORD_RESET_MINUTES": "15",
            "APP_PUBLIC_URL": "https://arranger.example/",
            "EMAIL_PROVIDER": "RESEND",
            "RESEND_API_KEY": "test-key",
            "PASSWORD_RESET_FROM": "Arranger <support@arranger.example>",
            "PASSWORD_RESET_SUBJECT": "Reset access",
        },
        check,
    )


def test_public_base_url_prefers_the_new_name_and_keeps_the_old_one_working():
    def both():
        settings = load_settings()
        assert settings.public_base_url == "https://new.example"
        assert settings.app_public_url == "https://new.example"  # read-only alias

    with_env(
        {"PUBLIC_BASE_URL": "https://new.example/", "APP_PUBLIC_URL": "https://old.example"},
        both,
    )

    def legacy_only():
        assert load_settings().public_base_url == "https://old.example"

    with_env({"APP_PUBLIC_URL": "https://old.example/"}, legacy_only)


def test_hardening_settings_have_safe_defaults():
    def check():
        settings = load_settings()
        assert settings.app_env == "development"
        assert settings.max_request_bytes == 1_000_000
        assert settings.max_upload_bytes == 40 * 1024 * 1024
        assert settings.upload_path_prefixes == ("/imports", "/projects")
        assert settings.trusted_proxy_count == 0
        assert settings.rate_limit_backend == "memory"
        assert settings.run_migrations_on_startup is True
        assert settings.allow_sqlite_in_production is False
        assert settings.session_idle_days == 14
        assert settings.session_touch_seconds == 300
        assert settings.require_verified_email is False
        assert settings.metrics_token == ""
        assert (settings.argon2_time_cost, settings.argon2_memory_kib) == (2, 19_456)
        assert "null" not in settings.cors_origins and "*" not in settings.cors_origins

    cleared = [
        "APP_ENV",
        "ARGON2_TIME_COST",
        "ARGON2_MEMORY_KIB",
        "ARGON2_PARALLELISM",
        "RATE_LIMIT_BACKEND",
        "REQUIRE_VERIFIED_EMAIL",
    ]
    old = {name: os.environ.pop(name) for name in cleared if name in os.environ}
    try:
        check()
    finally:
        os.environ.update(old)


def test_production_changes_the_defaults_that_matter():
    def check():
        settings = load_settings()
        assert settings.rate_limit_backend == "database"
        assert settings.require_verified_email is True
        assert settings.cookie_secure is True
        assert settings.cors_origins == []  # never defaulted in production
        assert settings.public_base_url == ""

    old = {
        name: os.environ.pop(name)
        for name in ("FRONTEND_ORIGINS", "PUBLIC_BASE_URL", "APP_PUBLIC_URL", "RATE_LIMIT_BACKEND")
        if name in os.environ
    }
    try:
        with_env({"APP_ENV": "production"}, check)
    finally:
        os.environ.update(old)


def test_new_settings_come_from_environment():
    def check():
        settings = load_settings()
        assert settings.max_upload_bytes == 123
        assert settings.upload_path_prefixes == ("/imports", "/audio")
        assert settings.trusted_proxy_count == 2
        assert settings.rate_limit_backend == "database"
        assert settings.run_migrations_on_startup is False
        assert settings.session_idle_days == 3
        assert settings.require_verified_email is True
        assert settings.metrics_token == "scrape-me"
        assert settings.db_pool_size == 9
        assert settings.database_url == "postgresql://u:p@db/x"
        assert settings.uses_postgres

    with_env(
        {
            "MAX_UPLOAD_BYTES": "123",
            "UPLOAD_PATH_PREFIXES": "/imports, /audio",
            "TRUSTED_PROXY_COUNT": "2",
            "RATE_LIMIT_BACKEND": "DATABASE",
            "RUN_MIGRATIONS_ON_STARTUP": "false",
            "SESSION_IDLE_DAYS": "3",
            "REQUIRE_VERIFIED_EMAIL": "true",
            "METRICS_TOKEN": "scrape-me",
            "DB_POOL_SIZE": "9",
            "DATABASE_URL": "postgresql://u:p@db/x",
        },
        check,
    )


# --- 18: production configuration validation ----------------------------------


def valid_production(**overrides):
    base = replace(
        load_settings(),
        app_env="production",
        artifact_backend="database",
        public_base_url="https://arranger.example",
        cors_origins=["https://arranger.example", "https://www.arranger.example"],
        cookie_secure=True,
        csrf_protection=True,
        database_url="postgresql://arranger:pw@db:5432/arranger",
        allow_sqlite_in_production=False,
        email_provider="resend",
        resend_api_key="re_placeholder",
        rate_limit_backend="database",
    )
    return replace(base, **overrides)


def test_valid_production_configuration_passes():
    assert configuration_problems(valid_production()) == []
    validate_settings(valid_production())


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"cors_origins": []}, "FRONTEND_ORIGINS must list"),
        ({"cors_origins": ["*"]}, "must not contain '*'"),
        ({"cors_origins": ["https://arranger.example", "null"]}, "must not contain 'null'"),
        ({"cors_origins": ["http://arranger.example"]}, "must be an https origin"),
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"csrf_protection": False}, "CSRF_PROTECTION"),
        ({"database_url": ""}, "ALLOW_SQLITE_IN_PRODUCTION"),
        ({"database_url": "sqlite:///data/app.db"}, "ALLOW_SQLITE_IN_PRODUCTION"),
        ({"email_provider": "console"}, "EMAIL_PROVIDER must be a real provider"),
        ({"resend_api_key": ""}, "RESEND_API_KEY is required"),
        ({"public_base_url": ""}, "PUBLIC_BASE_URL is required"),
        ({"public_base_url": "http://arranger.example"}, "PUBLIC_BASE_URL must be an https URL"),
    ],
)
def test_each_unsafe_production_setting_is_reported(overrides, fragment):
    problems = configuration_problems(valid_production(**overrides))
    assert len(problems) == 1, problems
    assert fragment in problems[0]
    with pytest.raises(ConfigurationError, match="1 problem"):
        validate_settings(valid_production(**overrides))


def test_sqlite_is_allowed_in_production_only_when_explicitly_permitted():
    blocked = valid_production(database_url="")
    assert configuration_problems(blocked)
    assert configuration_problems(replace(blocked, allow_sqlite_in_production=True)) == []


def test_production_problems_are_aggregated_into_one_clear_message():
    settings = valid_production(
        cors_origins=["*"], cookie_secure=False, csrf_protection=False, public_base_url=""
    )
    with pytest.raises(ConfigurationError) as excinfo:
        validate_settings(settings)
    message = str(excinfo.value)
    assert "4 problem(s)" in message
    for name in ("FRONTEND_ORIGINS", "COOKIE_SECURE", "CSRF_PROTECTION", "PUBLIC_BASE_URL"):
        assert name in message
    assert excinfo.value.problems and len(excinfo.value.problems) == 4


def test_development_is_not_held_to_production_rules():
    relaxed = replace(
        load_settings(),
        app_env="development",
        cookie_secure=False,
        cors_origins=["http://localhost:8000"],
        email_provider="console",
    )
    assert configuration_problems(relaxed) == []


def test_secrets_never_appear_in_the_configuration_error():
    settings = valid_production(cookie_secure=False, resend_api_key="re_live_super_secret")
    with pytest.raises(ConfigurationError) as excinfo:
        validate_settings(settings)
    assert "re_live_super_secret" not in str(excinfo.value)
    assert "pw@db" not in str(excinfo.value)


def test_production_env_example_documents_every_variable_and_passes_validation():
    example = Path(__file__).resolve().parents[1] / ".env.production.example"
    lines = example.read_text(encoding="utf-8").splitlines()
    values = {}
    previous = ""
    for line in lines:
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
            assert previous.startswith("#"), f"{name} needs a one-line comment above it"
        previous = line

    source = (Path(__file__).resolve().parents[1] / "src/arranger_api/settings.py").read_text()
    import re

    read_by_settings = set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', source)) - {
        "RAILWAY_GIT_COMMIT_SHA",
        "GITHUB_SHA",
        "APP_PUBLIC_URL",  # legacy alias of PUBLIC_BASE_URL
        "ARRANGER_DATABASE_URL",  # override of DATABASE_URL
        "ARRANGER_DB_PATH",  # SQLite only
        "ALLOW_SQLITE_IN_PRODUCTION",
        "INFO",
    }
    assert read_by_settings - set(values) == set()
    assert not any("re_live" in v or "sk_" in v for v in values.values())

    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(values)
    try:
        validate_settings(load_settings())
    finally:
        os.environ.clear()
        os.environ.update(old)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))


def test_local_artifacts_in_production_need_a_persistent_absolute_directory():
    relative = valid_production(artifact_backend="local", artifact_dir="data/artifacts")
    assert any("ARTIFACT_DIR" in p for p in configuration_problems(relative))
    # Near miss: an absolute path is the operator's explicit decision.
    absolute = valid_production(artifact_backend="local", artifact_dir=os.path.abspath("/srv/arranger"))
    assert configuration_problems(absolute) == []


def test_s3_backend_requires_its_credentials():
    problems = configuration_problems(valid_production(artifact_backend="s3"))
    assert any("S3_ENDPOINT" in p and "S3_SECRET_KEY" in p for p in problems)
    complete = valid_production(artifact_backend="s3", s3_endpoint="https://s3.example", s3_bucket="arranger",
                                s3_access_key="id", s3_secret_key="secret")
    assert configuration_problems(complete) == []


def test_unknown_artifact_backend_is_rejected_everywhere():
    assert any("ARTIFACT_BACKEND" in p for p in configuration_problems(valid_production(artifact_backend="ftp")))


def test_public_launch_requires_operator_details_for_the_legal_pages():
    problems = configuration_problems(valid_production(public_launch=True))
    assert len(problems) == 1 and "OPERATOR_NAME" in problems[0] and "DMCA_CONTACT_EMAIL" in problems[0]
    filled = valid_production(
        public_launch=True, operator_name="Example Ltd", operator_contact_email="help@example.com",
        operator_jurisdiction="England and Wales", privacy_contact_email="privacy@example.com",
        dmca_contact_email="copyright@example.com", data_region="EU",
    )
    assert configuration_problems(filled) == []
    # Near miss: a private deployment may leave them blank.
    assert configuration_problems(valid_production(public_launch=False)) == []


def test_model_repair_needs_a_spending_cap():
    assert any("MODEL_MAX_COST_USD" in p for p in configuration_problems(
        valid_production(model_repair_enabled=True, model_max_cost_usd=0.0)))
    assert configuration_problems(valid_production(model_repair_enabled=True, model_max_cost_usd=0.25)) == []
