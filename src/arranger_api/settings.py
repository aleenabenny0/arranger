"""Environment-driven API settings and production configuration validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

POSTGRES_SCHEMES = {"postgres", "postgresql"}
LOCAL_ENVIRONMENTS = {"development", "test"}
CONSOLE_EMAIL_PROVIDERS = {"console", "none", ""}


class ConfigurationError(RuntimeError):
    """Raised at startup when the configuration is unsafe or incomplete."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        lines = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(f"Invalid configuration ({len(self.problems)} problem(s)):\n{lines}")


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
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
    public_base_url: str
    host: str
    port: int
    reload: bool
    cookie_secure: bool
    session_days: int
    max_sessions_per_user: int
    csrf_protection: bool
    rate_limit_requests: int
    rate_limit_window_seconds: int
    auth_rate_limit_requests: int
    auth_rate_limit_window_seconds: int
    password_reset_rate_limit_requests: int
    password_reset_rate_limit_window_seconds: int
    max_request_bytes: int
    password_reset_minutes: int
    email_provider: str
    resend_api_key: str
    password_reset_from: str
    password_reset_subject: str
    cors_origins: list[str]
    frontend_dir: Path
    commit_sha: str
    # --- request limits -------------------------------------------------
    max_upload_bytes: int = 40 * 1024 * 1024
    upload_path_prefixes: tuple[str, ...] = ("/imports", "/projects")
    # --- client address -------------------------------------------------
    trusted_proxy_count: int = 0
    # --- rate limiting --------------------------------------------------
    rate_limit_backend: str = "memory"
    read_rate_limit_requests: int = 600
    read_rate_limit_window_seconds: int = 60
    upload_rate_limit_requests: int = 30
    upload_rate_limit_window_seconds: int = 60
    email_resend_rate_limit_requests: int = 3
    email_resend_rate_limit_window_seconds: int = 600
    rate_limit_max_buckets: int = 50_000
    # --- database -------------------------------------------------------
    database_url: str = ""
    sqlite_path: str = "data/arranger.db"
    run_migrations_on_startup: bool = True
    allow_sqlite_in_production: bool = False
    db_pool_size: int = 5
    db_pool_timeout_seconds: int = 10
    db_pool_max_lifetime_seconds: int = 1800
    db_statement_timeout_ms: int = 15_000
    sqlite_busy_timeout_ms: int = 5_000
    # --- sessions -------------------------------------------------------
    session_idle_days: int = 14
    session_touch_seconds: int = 300
    cleanup_interval_seconds: int = 3600
    # --- passwords ------------------------------------------------------
    argon2_time_cost: int = 2
    argon2_memory_kib: int = 19_456
    argon2_parallelism: int = 1
    # --- email verification ---------------------------------------------
    require_verified_email: bool = False
    email_verification_minutes: int = 60 * 24
    email_verification_subject: str = "Verify your Arranger email address"
    # --- observability --------------------------------------------------
    metrics_token: str = ""
    # --- files ----------------------------------------------------------
    artifact_backend: str = "local"            # local | database | s3
    artifact_dir: str = "data/artifacts"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = "auto"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    # Uploaded originals are only needed until they are parsed; generated files
    # can always be rebuilt from the saved arrangement. 0 keeps them forever.
    upload_retention_days: int = 30
    export_retention_days: int = 0
    # --- background jobs --------------------------------------------------
    job_workers: int = 1                        # 0: run `python -m arranger_api.worker` instead
    job_lease_seconds: int = 120
    job_max_attempts: int = 2
    job_retention_days: int = 30
    # --- job queue ---------------------------------------------------------
    # "database": workers poll the jobs table (the default, one host or a few).
    # "sqs": the API sends a message per job and workers long-poll the queue.
    # "memory": one process, for tests and development.
    job_queue_backend: str = "database"
    sqs_queue_url: str = ""
    sqs_dead_letter_queue_url: str = ""
    aws_endpoint_url: str = ""                  # LocalStack or another SQS-compatible endpoint
    aws_region: str = ""
    # How long a received message stays invisible; 0 derives it from the
    # slowest job's time limit plus a margin.
    job_visibility_seconds: int = 0
    job_queue_max_receives: int = 3             # deliveries before a message is dead-lettered
    arrange_max_seconds: int = 180
    engrave_max_seconds: int = 120
    transcribe_max_seconds: int = 900
    # --- per-user quotas ---------------------------------------------------
    quota_projects: int = 200
    quota_storage_bytes: int = 500 * 1024 * 1024
    quota_jobs_per_day: int = 200
    quota_transcriptions_per_day: int = 20
    quota_active_jobs: int = 3
    # --- model-assisted repair (off unless explicitly enabled) --------------
    model_repair_enabled: bool = False
    model_name: str = ""
    model_max_attempts: int = 3
    model_max_cost_usd: float = 0.25
    # --- who runs this service: shown on the legal pages ---------------------
    # Deliberately no defaults. A policy page with an invented company name is
    # worse than one that says the detail has not been provided.
    operator_name: str = ""
    operator_contact_email: str = ""
    operator_postal_address: str = ""
    operator_jurisdiction: str = ""
    privacy_contact_email: str = ""
    dmca_contact_email: str = ""
    data_region: str = ""
    public_launch: bool = False

    @property
    def app_public_url(self) -> str:
        """Backwards-compatible name for `public_base_url`."""
        return self.public_base_url

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_local(self) -> bool:
        return self.app_env in LOCAL_ENVIRONMENTS

    @property
    def effective_visibility_seconds(self) -> int:
        """How long a queue message stays invisible to other workers once received.

        Longer than the slowest job can run, plus a margin for saving its
        result, so a job is never handed to a second worker while the first is
        still on it.
        """
        if self.job_visibility_seconds > 0:
            return self.job_visibility_seconds
        return max(self.arrange_max_seconds, self.engrave_max_seconds, self.transcribe_max_seconds) + 60

    @property
    def uses_postgres(self) -> bool:
        if not self.database_url:
            return False
        return urlsplit(self.database_url).scheme in POSTGRES_SCHEMES

    @property
    def allowed_origins(self) -> frozenset[str]:
        """Origins accepted by the unsafe-request Origin/Referer check."""
        origins = {normalize_origin(origin) for origin in self.cors_origins}
        origins.add(normalize_origin(self.public_base_url))
        origins.discard("")
        return frozenset(origins)


def normalize_origin(value: str | None) -> str:
    """Reduce a URL or Origin header to `scheme://host[:port]`, lowercased.

    Returns "" for anything that is not an http(s) origin, so callers can treat
    "null", "*", garbage, and missing values identically.
    """
    value = (value or "").strip()
    if not value or value in {"null", "*"}:
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    default_port = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == default_port:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development").strip().lower() or "development"
    production = app_env == "production"
    source_root = Path(__file__).resolve().parents[2]
    # The web app is built by Vite into frontend-react/dist (`npm run build`).
    cwd_frontend = Path.cwd() / "frontend-react" / "dist"
    default_frontend = cwd_frontend if cwd_frontend.exists() else source_root / "frontend-react" / "dist"
    port = env_int("PORT", 8000)
    public_base_url = (
        (
            os.environ.get("PUBLIC_BASE_URL")
            or os.environ.get("APP_PUBLIC_URL")
            or ("" if production else "http://127.0.0.1:8000")
        )
        .strip()
        .rstrip("/")
    )
    # Production never gets a default origin list: it must be stated.
    default_origins = (
        []
        if production
        else list(
            dict.fromkeys(
                [
                    "http://127.0.0.1:8000",
                    "http://localhost:8000",
                    f"http://127.0.0.1:{port}",
                    f"http://localhost:{port}",
                ]
            )
        )
    )
    return Settings(
        app_env=app_env,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        public_base_url=public_base_url,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=port,
        reload=env_bool("RELOAD", not production),
        cookie_secure=env_bool("COOKIE_SECURE", production),
        session_days=env_int("SESSION_DAYS", 30),
        max_sessions_per_user=env_int("MAX_SESSIONS_PER_USER", 5),
        csrf_protection=env_bool("CSRF_PROTECTION", True),
        rate_limit_requests=env_int("RATE_LIMIT_REQUESTS", 120),
        rate_limit_window_seconds=env_int("RATE_LIMIT_WINDOW_SECONDS", 60),
        auth_rate_limit_requests=env_int("AUTH_RATE_LIMIT_REQUESTS", 20),
        auth_rate_limit_window_seconds=env_int("AUTH_RATE_LIMIT_WINDOW_SECONDS", 60),
        password_reset_rate_limit_requests=env_int("PASSWORD_RESET_RATE_LIMIT_REQUESTS", 5),
        password_reset_rate_limit_window_seconds=env_int(
            "PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS",
            300,
        ),
        max_request_bytes=env_int("MAX_REQUEST_BYTES", 1_000_000),
        password_reset_minutes=env_int("PASSWORD_RESET_MINUTES", 30),
        email_provider=os.environ.get("EMAIL_PROVIDER", "console").strip().lower(),
        resend_api_key=os.environ.get("RESEND_API_KEY", "").strip(),
        password_reset_from=os.environ.get(
            "PASSWORD_RESET_FROM",
            "Arranger <no-reply@arranger.local>",
        ),
        password_reset_subject=os.environ.get(
            "PASSWORD_RESET_SUBJECT",
            "Reset your Arranger password",
        ),
        cors_origins=env_list("FRONTEND_ORIGINS", default_origins),
        frontend_dir=Path(os.environ.get("FRONTEND_DIR", default_frontend)),
        commit_sha=(
            os.environ.get("RAILWAY_GIT_COMMIT_SHA")
            or os.environ.get("GITHUB_SHA")
            or os.environ.get("COMMIT_SHA")
            or "unknown"
        ),
        max_upload_bytes=env_int("MAX_UPLOAD_BYTES", 40 * 1024 * 1024),
        upload_path_prefixes=tuple(env_list("UPLOAD_PATH_PREFIXES", ["/imports", "/projects"])),
        trusted_proxy_count=max(0, env_int("TRUSTED_PROXY_COUNT", 0)),
        rate_limit_backend=os.environ.get(
            "RATE_LIMIT_BACKEND", "database" if production else "memory"
        )
        .strip()
        .lower(),
        read_rate_limit_requests=env_int("READ_RATE_LIMIT_REQUESTS", 600),
        read_rate_limit_window_seconds=env_int("READ_RATE_LIMIT_WINDOW_SECONDS", 60),
        upload_rate_limit_requests=env_int("UPLOAD_RATE_LIMIT_REQUESTS", 30),
        upload_rate_limit_window_seconds=env_int("UPLOAD_RATE_LIMIT_WINDOW_SECONDS", 60),
        email_resend_rate_limit_requests=env_int("EMAIL_RESEND_RATE_LIMIT_REQUESTS", 3),
        email_resend_rate_limit_window_seconds=env_int(
            "EMAIL_RESEND_RATE_LIMIT_WINDOW_SECONDS", 600
        ),
        rate_limit_max_buckets=env_int("RATE_LIMIT_MAX_BUCKETS", 50_000),
        database_url=(
            os.environ.get("ARRANGER_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
        ).strip(),
        sqlite_path=os.environ.get("ARRANGER_DB_PATH", "data/arranger.db"),
        run_migrations_on_startup=env_bool("RUN_MIGRATIONS_ON_STARTUP", True),
        allow_sqlite_in_production=env_bool("ALLOW_SQLITE_IN_PRODUCTION", False),
        db_pool_size=max(1, env_int("DB_POOL_SIZE", 5)),
        db_pool_timeout_seconds=env_int("DB_POOL_TIMEOUT_SECONDS", 10),
        db_pool_max_lifetime_seconds=env_int("DB_POOL_MAX_LIFETIME_SECONDS", 1800),
        db_statement_timeout_ms=env_int("DB_STATEMENT_TIMEOUT_MS", 15_000),
        sqlite_busy_timeout_ms=env_int("SQLITE_BUSY_TIMEOUT_MS", 5_000),
        session_idle_days=env_int("SESSION_IDLE_DAYS", 14),
        session_touch_seconds=env_int("SESSION_TOUCH_SECONDS", 300),
        cleanup_interval_seconds=env_int("CLEANUP_INTERVAL_SECONDS", 3600),
        argon2_time_cost=env_int("ARGON2_TIME_COST", 2),
        argon2_memory_kib=env_int("ARGON2_MEMORY_KIB", 19_456),
        argon2_parallelism=env_int("ARGON2_PARALLELISM", 1),
        require_verified_email=env_bool("REQUIRE_VERIFIED_EMAIL", production),
        email_verification_minutes=env_int("EMAIL_VERIFICATION_MINUTES", 60 * 24),
        email_verification_subject=os.environ.get(
            "EMAIL_VERIFICATION_SUBJECT",
            "Verify your Arranger email address",
        ),
        metrics_token=os.environ.get("METRICS_TOKEN", "").strip(),
        # Production defaults to the database: a container's disk does not survive a deploy.
        artifact_backend=os.environ.get("ARTIFACT_BACKEND", "database" if production else "local")
        .strip()
        .lower(),
        artifact_dir=os.environ.get("ARTIFACT_DIR", "data/artifacts"),
        s3_endpoint=os.environ.get("S3_ENDPOINT", "").strip(),
        s3_bucket=os.environ.get("S3_BUCKET", "").strip(),
        s3_region=os.environ.get("S3_REGION", "auto").strip(),
        s3_access_key=os.environ.get("S3_ACCESS_KEY", "").strip(),
        s3_secret_key=os.environ.get("S3_SECRET_KEY", "").strip(),
        upload_retention_days=max(0, env_int("UPLOAD_RETENTION_DAYS", 30)),
        export_retention_days=max(0, env_int("EXPORT_RETENTION_DAYS", 0)),
        job_workers=max(0, env_int("JOB_WORKERS", 1)),
        job_lease_seconds=max(10, env_int("JOB_LEASE_SECONDS", 120)),
        job_max_attempts=max(1, env_int("JOB_MAX_ATTEMPTS", 2)),
        job_retention_days=max(1, env_int("JOB_RETENTION_DAYS", 30)),
        job_queue_backend=os.environ.get("JOB_QUEUE_BACKEND", "database").strip().lower() or "database",
        sqs_queue_url=os.environ.get("SQS_QUEUE_URL", "").strip(),
        sqs_dead_letter_queue_url=os.environ.get("SQS_DEAD_LETTER_QUEUE_URL", "").strip(),
        aws_endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "").strip(),
        # Empty lets boto3 use its own AWS_DEFAULT_REGION or the instance's region.
        aws_region=os.environ.get("AWS_REGION", "").strip(),
        job_visibility_seconds=max(0, env_int("JOB_VISIBILITY_SECONDS", 0)),
        job_queue_max_receives=max(1, env_int("JOB_QUEUE_MAX_RECEIVES", 3)),
        arrange_max_seconds=max(5, env_int("ARRANGE_MAX_SECONDS", 180)),
        engrave_max_seconds=max(5, env_int("ENGRAVE_MAX_SECONDS", 120)),
        transcribe_max_seconds=max(5, env_int("TRANSCRIBE_MAX_SECONDS", 900)),
        quota_projects=max(1, env_int("QUOTA_PROJECTS", 200)),
        quota_storage_bytes=max(1, env_int("QUOTA_STORAGE_BYTES", 500 * 1024 * 1024)),
        quota_jobs_per_day=max(1, env_int("QUOTA_JOBS_PER_DAY", 200)),
        quota_transcriptions_per_day=max(0, env_int("QUOTA_TRANSCRIPTIONS_PER_DAY", 20)),
        quota_active_jobs=max(1, env_int("QUOTA_ACTIVE_JOBS", 3)),
        model_repair_enabled=env_bool("MODEL_REPAIR_ENABLED", False),
        model_name=os.environ.get("ARRANGER_MODEL", "").strip(),
        model_max_attempts=max(1, min(8, env_int("MODEL_MAX_ATTEMPTS", 3))),
        model_max_cost_usd=max(0.0, env_float("MODEL_MAX_COST_USD", 0.25)),
        operator_name=os.environ.get("OPERATOR_NAME", "").strip(),
        operator_contact_email=os.environ.get("OPERATOR_CONTACT_EMAIL", "").strip(),
        operator_postal_address=os.environ.get("OPERATOR_POSTAL_ADDRESS", "").strip(),
        operator_jurisdiction=os.environ.get("OPERATOR_JURISDICTION", "").strip(),
        privacy_contact_email=os.environ.get("PRIVACY_CONTACT_EMAIL", "").strip(),
        dmca_contact_email=os.environ.get("DMCA_CONTACT_EMAIL", "").strip(),
        data_region=os.environ.get("DATA_REGION", "").strip(),
        public_launch=env_bool("PUBLIC_LAUNCH", False),
    )


def _is_https_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def configuration_problems(settings: Settings) -> list[str]:
    """Every reason this configuration must not serve traffic.

    Only production is held to the deployment rules; development and test stay
    permissive so a fresh checkout runs with no environment at all.
    """
    problems: list[str] = []
    if settings.rate_limit_backend not in {"memory", "database"}:
        problems.append("RATE_LIMIT_BACKEND must be 'memory' or 'database'.")
    if settings.job_queue_backend not in {"database", "memory", "sqs"}:
        problems.append("JOB_QUEUE_BACKEND must be 'database', 'memory' or 'sqs'.")
    if settings.job_queue_backend == "sqs" and not settings.sqs_queue_url:
        problems.append("JOB_QUEUE_BACKEND=sqs requires SQS_QUEUE_URL.")
    if settings.is_production and settings.job_queue_backend == "memory":
        problems.append("JOB_QUEUE_BACKEND=memory lives in one process; jobs would be lost on restart.")
    if settings.artifact_backend not in {"local", "database", "s3"}:
        problems.append("ARTIFACT_BACKEND must be 'local', 'database' or 's3'.")
    if settings.artifact_backend == "s3":
        missing = [
            name for name, value in (
                ("S3_ENDPOINT", settings.s3_endpoint), ("S3_BUCKET", settings.s3_bucket),
                ("S3_ACCESS_KEY", settings.s3_access_key), ("S3_SECRET_KEY", settings.s3_secret_key),
            ) if not value
        ]
        if missing:
            problems.append("ARTIFACT_BACKEND=s3 requires " + ", ".join(missing) + ".")
    if settings.email_provider not in CONSOLE_EMAIL_PROVIDERS | {"resend"}:
        problems.append(f"EMAIL_PROVIDER '{settings.email_provider}' is not supported.")
    if not settings.is_production:
        return problems

    if not settings.cors_origins:
        problems.append("FRONTEND_ORIGINS must list the exact https origins of the frontend.")
    for origin in settings.cors_origins:
        if origin in {"*", "null"}:
            problems.append(f"FRONTEND_ORIGINS must not contain '{origin}'.")
        elif not _is_https_url(origin):
            problems.append(f"FRONTEND_ORIGINS entry '{origin}' must be an https origin.")
    if not settings.cookie_secure:
        problems.append("COOKIE_SECURE must be true in production.")
    if not settings.csrf_protection:
        problems.append("CSRF_PROTECTION must be true in production.")
    if not settings.uses_postgres and not settings.allow_sqlite_in_production:
        problems.append(
            "DATABASE_URL must be a Postgres URL in production "
            "(set ALLOW_SQLITE_IN_PRODUCTION=true to override)."
        )
    if settings.email_provider in CONSOLE_EMAIL_PROVIDERS:
        problems.append("EMAIL_PROVIDER must be a real provider in production, not console.")
    if settings.email_provider == "resend" and not settings.resend_api_key:
        problems.append("RESEND_API_KEY is required when EMAIL_PROVIDER=resend.")
    if not settings.public_base_url:
        problems.append("PUBLIC_BASE_URL is required in production.")
    elif not _is_https_url(settings.public_base_url):
        problems.append("PUBLIC_BASE_URL must be an https URL in production.")
    if settings.artifact_backend == "local":
        # Not unsafe, but it must be a decision: a container's own disk is wiped
        # on every deploy, and then every download link is dead.
        if not os.path.isabs(settings.artifact_dir):
            problems.append(
                "ARTIFACT_BACKEND=local needs an absolute ARTIFACT_DIR on a persistent volume "
                "in production (or use ARTIFACT_BACKEND=database or s3)."
            )
    if settings.model_repair_enabled and settings.model_max_cost_usd <= 0:
        problems.append("MODEL_REPAIR_ENABLED=true needs MODEL_MAX_COST_USD above zero as a spending cap.")
    if settings.public_launch:
        # The legal pages print these. Publishing them blank is publishing a
        # privacy policy with nobody responsible for it.
        missing = [
            name for name, value in (
                ("OPERATOR_NAME", settings.operator_name),
                ("OPERATOR_CONTACT_EMAIL", settings.operator_contact_email),
                ("OPERATOR_JURISDICTION", settings.operator_jurisdiction),
                ("PRIVACY_CONTACT_EMAIL", settings.privacy_contact_email),
                ("DMCA_CONTACT_EMAIL", settings.dmca_contact_email),
                ("DATA_REGION", settings.data_region),
            ) if not value
        ]
        if missing:
            problems.append("PUBLIC_LAUNCH=true requires " + ", ".join(missing) + " for the legal pages.")
    return problems


def validate_settings(settings: Settings) -> None:
    """Fail fast with one aggregated message when the configuration is unsafe."""
    problems = configuration_problems(settings)
    if problems:
        raise ConfigurationError(problems)
