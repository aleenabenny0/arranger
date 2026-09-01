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
    host: str
    port: int
    reload: bool
    cookie_secure: bool
    session_days: int
    cors_origins: list[str]
    frontend_dir: Path


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development")
    source_root = Path(__file__).resolve().parents[2]
    cwd_frontend = Path.cwd() / "frontend"
    default_frontend = cwd_frontend if cwd_frontend.exists() else source_root / "frontend"
    return Settings(
        app_env=app_env,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=env_int("PORT", 8000),
        reload=env_bool("RELOAD", app_env != "production"),
        cookie_secure=env_bool("COOKIE_SECURE", app_env == "production"),
        session_days=env_int("SESSION_DAYS", 30),
        cors_origins=env_list(
            "FRONTEND_ORIGINS",
            ["http://127.0.0.1:8000", "http://localhost:8000", "null"],
        ),
        frontend_dir=Path(os.environ.get("FRONTEND_DIR", default_frontend)),
    )
