"""Environment-driven API settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    return int(value)


def env_list(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    app_public_url: str
    host: str
    port: int
    reload: bool
    cookie_secure: bool
    session_days: int
    max_sessions_per_user: int
    csrf_protection: bool
    rate_limit_requests: int
    rate_limit_window_seconds: int
    max_request_bytes: int
    password_reset_minutes: int
    email_provider: str
    resend_api_key: str
    password_reset_from: str
    password_reset_subject: str
    cors_origins: list[str]
    frontend_dir: Path


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development")
    source_root = Path(__file__).resolve().parents[2]
    cwd_frontend = Path.cwd() / "frontend"
    default_frontend = cwd_frontend if cwd_frontend.exists() else source_root / "frontend"
    return Settings(
        app_env=app_env,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        app_public_url=os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/"),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=env_int("PORT", 8000),
        reload=env_bool("RELOAD", app_env != "production"),
        cookie_secure=env_bool("COOKIE_SECURE", app_env == "production"),
        session_days=env_int("SESSION_DAYS", 30),
        max_sessions_per_user=env_int("MAX_SESSIONS_PER_USER", 5),
        csrf_protection=env_bool("CSRF_PROTECTION", True),
        rate_limit_requests=env_int("RATE_LIMIT_REQUESTS", 120),
        rate_limit_window_seconds=env_int("RATE_LIMIT_WINDOW_SECONDS", 60),
        max_request_bytes=env_int("MAX_REQUEST_BYTES", 1_000_000),
        password_reset_minutes=env_int("PASSWORD_RESET_MINUTES", 30),
        email_provider=os.environ.get("EMAIL_PROVIDER", "console").strip().lower(),
        resend_api_key=os.environ.get("RESEND_API_KEY", ""),
        password_reset_from=os.environ.get(
            "PASSWORD_RESET_FROM",
            "Arranger <no-reply@arranger.local>",
        ),
        password_reset_subject=os.environ.get(
            "PASSWORD_RESET_SUBJECT",
            "Reset your Arranger password",
        ),
        cors_origins=env_list(
            "FRONTEND_ORIGINS",
            ["http://127.0.0.1:8000", "http://localhost:8000", "null"],
        ),
        frontend_dir=Path(os.environ.get("FRONTEND_DIR", default_frontend)),
    )
