"""FastAPI dependencies shared by `main`, the auth routes and feature routers.

Everything a router needs comes from `request.app.state`, populated once by
`create_app`:

    settings       Settings
    database       storage.Database        (connections; use `get_storage`)
    rate_limits    security.RateLimits
    passwords      auth.PasswordService
    email_sender   email.EmailSender
    email_health   email.EmailHealth
    metrics        metrics.AppMetrics

Routers should depend on the functions below rather than reaching into
`app.state` themselves, so tests can swap them with `dependency_overrides`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Cookie, Depends, Request

from .auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    CurrentUser,
    PasswordService,
    forbidden,
    hash_token,
    tokens_match,
    unauthorized,
)
from .email import EmailHealth, EmailSender
from .errors import service_unavailable
from .metrics import AppMetrics
from .observability import get_logger
from .security import UNSAFE_METHODS, RateLimiter, RateLimits, client_ip
from .settings import Settings
from .storage import Database, Storage

logger = get_logger("arranger_api.deps")

# Liveness must not depend on anything, including a database-backed limiter.
RATE_LIMIT_EXEMPT_ROUTES = frozenset({"/health"})


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_metrics(request: Request) -> AppMetrics:
    return request.app.state.metrics


def get_rate_limits(request: Request) -> RateLimits:
    return request.app.state.rate_limits


def get_passwords(request: Request) -> PasswordService:
    return request.app.state.passwords


def get_email_sender(request: Request) -> EmailSender:
    """The sender built at startup. There is no fallback provider."""
    return request.app.state.email_sender


def get_email_health(request: Request) -> EmailHealth:
    return request.app.state.email_health


def get_storage(request: Request) -> Iterator[Storage]:
    """One managed connection per request, wrapped in a `Storage`.

    Migrations are not run here; they run once at startup. On any error the
    connection is rolled back before it goes back to the pool, so a failed
    request can never leak a half-finished transaction into the next one.
    """
    database: Database = request.app.state.database
    try:
        conn = database.acquire()
    except Exception as exc:
        logger.error("database_unavailable", exc_info=exc)
        raise service_unavailable("database_unavailable") from exc
    storage = Storage(conn, dialect=database.dialect)
    try:
        yield storage
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            logger.warning("rollback_failed", exc_info=True)
        raise
    finally:
        database.release(conn)


def route_template(request: Request) -> str:
    """The matched route's path template, e.g. `/scores/{score_id}`."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


def enforce_rate_limit(request: Request) -> None:
    """Application-wide dependency: limit by client IP and route template.

    Keyed on the template, not the raw path, so `/scores/<any id>` is one
    bucket. The chosen limiter is remembered on `request.state` so
    `get_current_user` can apply the same budget per user id.
    """
    limits: RateLimits | None = getattr(request.app.state, "rate_limits", None)
    if limits is None:
        return
    template = route_template(request)
    if template in RATE_LIMIT_EXEMPT_ROUTES:
        return
    limiter = limits.for_request(request.method, template)
    request.state.rate_limiter = limiter
    request.state.route_template = template
    limiter.check(f"ip:{client_ip(request)}:{request.method}:{template}")


def rate_limit(name: str, requests: int, window_seconds: int, *, per: str = "ip") -> Callable:
    """Dependency factory for a route-specific budget on top of the global one.

        @router.post("/imports", dependencies=[Depends(rate_limit("imports", 10, 60))])

    `per="ip"` keys on the client address; `per="user"` keys on the signed-in
    user (and therefore requires authentication).
    """
    if per not in {"ip", "user"}:
        raise ValueError("per must be 'ip' or 'user'")

    def _limiter(request: Request) -> RateLimiter:
        return request.app.state.rate_limits.limiter(name, requests, window_seconds)

    if per == "user":

        def check_user(request: Request, user: CurrentUser = Depends(get_current_user)) -> None:
            _limiter(request).check(f"user:{user.id}")

        return check_user

    def check_ip(request: Request) -> None:
        _limiter(request).check(f"ip:{client_ip(request)}")

    return check_ip


def _count_auth_failure(request: Request, reason: str) -> None:
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics.auth_failures.inc(reason=reason)


def get_current_session(
    request: Request,
    storage: Storage = Depends(get_storage),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """The live session (joined with its user) for this request, or 401.

    For unsafe methods the `X-CSRF-Token` header must hash to the
    `csrf_token_hash` stored on *this* session. A token minted for another
    session, or a cookie/header pair an attacker planted, does not match.
    """
    settings: Settings = request.app.state.settings
    if not session_token:
        _count_auth_failure(request, "no_session")
        raise unauthorized()
    session = storage.active_session(
        hash_token(session_token),
        idle_seconds=settings.session_idle_days * 24 * 60 * 60,
        touch_seconds=settings.session_touch_seconds,
    )
    if session is None:
        _count_auth_failure(request, "invalid_session")
        raise unauthorized()
    if settings.csrf_protection and request.method in UNSAFE_METHODS:
        if not tokens_match(request.headers.get(CSRF_HEADER), session.get("csrf_token_hash")):
            _count_auth_failure(request, "csrf")
            raise forbidden("Missing or invalid CSRF token.", "csrf_failed")
    limiter: RateLimiter | None = getattr(request.state, "rate_limiter", None)
    if limiter is not None:
        template = getattr(request.state, "route_template", "unmatched")
        limiter.check(f"user:{session['id']}:{request.method}:{template}")
    return session


def get_current_user(session: dict = Depends(get_current_session)) -> CurrentUser:
    return CurrentUser(
        id=session["id"],
        email=session["email"],
        display_name=session["display_name"],
        email_verified=bool(session.get("email_verified_at")),
        session_id=session["session_id"],
    )


def require_verified_user(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Gate for expensive operations: a signed-in user with a verified address.

    A no-op check when `REQUIRE_VERIFIED_EMAIL` is false (the default outside
    production), so local development never needs a mailbox.
    """
    settings: Settings = request.app.state.settings
    if settings.require_verified_email and not user.email_verified:
        _count_auth_failure(request, "email_not_verified")
        raise forbidden(
            "Verify your email address to use this feature.",
            "email_not_verified",
        )
    return user
