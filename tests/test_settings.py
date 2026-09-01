"""Environment settings tests."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger_api.settings import load_settings  # noqa: E402


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

    with_env(
        {"APP_ENV": "production", "PORT": "9999", "COOKIE_SECURE": "true", "RELOAD": "false"},
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
        assert settings.password_reset_minutes == 15

    with_env(
        {
            "MAX_SESSIONS_PER_USER": "2",
            "CSRF_PROTECTION": "false",
            "RATE_LIMIT_REQUESTS": "10",
            "RATE_LIMIT_WINDOW_SECONDS": "5",
            "PASSWORD_RESET_MINUTES": "15",
        },
        check,
    )


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
