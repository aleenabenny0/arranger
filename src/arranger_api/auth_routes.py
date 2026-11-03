"""Account endpoints: registration, sessions, passwords, email verification."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Request, Response

from .auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    CurrentUser,
    PasswordService,
    clear_session_cookie,
    forbidden,
    hash_token,
    new_token,
    password_problems,
    set_csrf_cookie,
    set_session_cookie,
    tokens_match,
    unauthorized,
)
from .deps import (
    get_current_user,
    get_email_health,
    get_email_sender,
    get_metrics,
    get_passwords,
    get_rate_limits,
    get_settings,
    get_storage,
)
from .email import (
    EmailHealth,
    EmailSender,
    build_email_verification_link,
    build_password_reset_link,
    deliver_email,
    email_domain,
)
from .errors import api_error, not_found
from .metrics import AppMetrics
from .observability import get_logger, log_event
from .schemas import (
    EmailVerifyRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RegisterRequest,
    SessionsResponse,
    UserResponse,
)
from .security import RateLimits, client_ip, coarse_ip
from .settings import Settings
from .storage import INTEGRITY_ERRORS, Storage

logger = get_logger("arranger_api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])


def user_payload(user: dict | CurrentUser) -> dict:
    if isinstance(user, CurrentUser):
        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "email_verified": user.email_verified,
        }
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "email_verified": bool(user.get("email_verified_at")),
    }


def _weak_password(problems: list[str]):
    return api_error(400, "weak_password", " ".join(problems))


def start_session(
    *,
    request: Request,
    response: Response,
    storage: Storage,
    settings: Settings,
    user_id: str,
) -> dict:
    """Create a session row and set the session and CSRF cookies."""
    token = new_token()
    csrf_token = new_token()
    max_age = 60 * 60 * 24 * settings.session_days
    session = storage.create_session(
        user_id,
        hash_token(token),
        settings.session_days,
        csrf_token_hash=hash_token(csrf_token),
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        max_sessions=settings.max_sessions_per_user,
    )
    set_session_cookie(response, token, secure=settings.cookie_secure, max_age=max_age)
    set_csrf_cookie(response, csrf_token, secure=settings.cookie_secure, max_age=max_age)
    return session


def _revoke_presented_session(storage: Storage, session_token: str | None) -> None:
    """Session fixation defence: whatever session the browser arrived with is
    dead once it authenticates; the new identity always gets a new token."""
    if session_token:
        storage.revoke_session(hash_token(session_token))


def _queue_verification_email(
    *,
    background: BackgroundTasks,
    storage: Storage,
    settings: Settings,
    email_sender: EmailSender,
    health: EmailHealth,
    metrics: AppMetrics,
    user_id: str,
    email: str,
) -> None:
    token = new_token()
    storage.create_email_verification_token(
        user_id, hash_token(token), settings.email_verification_minutes
    )
    link = build_email_verification_link(settings, token)
    minutes = settings.email_verification_minutes
    background.add_task(
        deliver_email,
        "email_verification",
        lambda: email_sender.send_email_verification(email, link, minutes),
        provider=settings.email_provider,
        recipient=email,
        health=health,
        metrics=metrics,
    )


@router.post("/register", response_model=UserResponse)
def register_endpoint(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    passwords: PasswordService = Depends(get_passwords),
    email_sender: EmailSender = Depends(get_email_sender),
    health: EmailHealth = Depends(get_email_health),
    metrics: AppMetrics = Depends(get_metrics),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    if problems := password_problems(payload.password, payload.email):
        raise _weak_password(problems)
    password_hash = passwords.hash(payload.password)
    display_name = payload.display_name or payload.email.split("@", 1)[0]
    try:
        with storage.transaction():
            _revoke_presented_session(storage, session_token)
            user = storage.create_user(payload.email, password_hash, display_name)
            start_session(
                request=request,
                response=response,
                storage=storage,
                settings=settings,
                user_id=user["id"],
            )
            _queue_verification_email(
                background=background,
                storage=storage,
                settings=settings,
                email_sender=email_sender,
                health=health,
                metrics=metrics,
                user_id=user["id"],
                email=user["email"],
            )
    except INTEGRITY_ERRORS as exc:
        raise api_error(400, "invalid_input", "email is already registered") from exc
    return {"user": user_payload(user)}


@router.post("/login", response_model=UserResponse)
def login_endpoint(
    payload: LoginRequest,
    request: Request,
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    passwords: PasswordService = Depends(get_passwords),
    metrics: AppMetrics = Depends(get_metrics),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    user = storage.get_user_with_password(payload.email)
    if user is None:
        # Same work as a wrong password, so timing does not reveal the account.
        passwords.verify_dummy(payload.password)
        metrics.auth_failures.inc(reason="bad_credentials")
        raise unauthorized()
    check = passwords.verify(payload.password, user["password_hash"])
    if not check.ok:
        metrics.auth_failures.inc(reason="bad_credentials")
        raise unauthorized()
    new_hash = passwords.hash(payload.password) if check.needs_rehash else None
    with storage.transaction():
        if new_hash is not None:
            storage.update_password_hash(user["id"], new_hash)
        _revoke_presented_session(storage, session_token)
        start_session(
            request=request,
            response=response,
            storage=storage,
            settings=settings,
            user_id=user["id"],
        )
    if new_hash is not None:
        log_event(logger, "password_hash_upgraded", user_id=user["id"])
    return {"user": user_payload(user)}


@router.post("/logout")
def logout_endpoint(
    request: Request,
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    metrics: AppMetrics = Depends(get_metrics),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    if session_token:
        token_hash = hash_token(session_token)
        session = storage.session_for_token(token_hash)
        if session is not None:
            if settings.csrf_protection and not tokens_match(
                request.headers.get(CSRF_HEADER), session.get("csrf_token_hash")
            ):
                metrics.auth_failures.inc(reason="csrf")
                raise forbidden("Missing or invalid CSRF token.", "csrf_failed")
            storage.revoke_session(token_hash)
    clear_session_cookie(response, secure=settings.cookie_secure)
    return {"logged_out": True}


@router.post("/logout-all")
def logout_all_endpoint(
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    revoked = storage.revoke_user_sessions(user.id)
    clear_session_cookie(response, secure=settings.cookie_secure)
    return {"logged_out": True, "revoked_sessions": revoked}


@router.get("/me", response_model=UserResponse)
def me_endpoint(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"user": user_payload(user)}


@router.get("/sessions", response_model=SessionsResponse)
def list_sessions_endpoint(
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    rows = storage.list_sessions(user.id, idle_seconds=settings.session_idle_days * 86_400)
    return {
        "sessions": [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "expires_at": row["expires_at"],
                "user_agent": (row.get("user_agent") or "")[:200],
                "ip": coarse_ip(row.get("ip_address")),
                "current": row["id"] == user.session_id,
            }
            for row in rows
        ]
    }


@router.delete("/sessions/{session_id}")
def revoke_session_endpoint(
    session_id: str,
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not storage.revoke_session_by_id(user.id, session_id):
        raise not_found("session", session_id)
    if session_id == user.session_id:
        clear_session_cookie(response, secure=settings.cookie_secure)
    return {"revoked": True, "current": session_id == user.session_id}


@router.post("/password/change", response_model=UserResponse)
def change_password_endpoint(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    passwords: PasswordService = Depends(get_passwords),
    metrics: AppMetrics = Depends(get_metrics),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_user_with_password_by_id(user.id)
    if record is None or not passwords.verify(payload.current_password, record["password_hash"]).ok:
        metrics.auth_failures.inc(reason="bad_current_password")
        raise forbidden("Current password is incorrect.", "invalid_credentials")
    if problems := password_problems(payload.new_password, user.email):
        raise _weak_password(problems)
    if payload.new_password == payload.current_password:
        raise _weak_password(["New password must differ from the current password."])
    new_hash = passwords.hash(payload.new_password)
    with storage.transaction():
        storage.update_password_hash(user.id, new_hash)
        # Every session goes, including this one; the caller continues on a
        # freshly minted session so a stolen cookie stops working immediately.
        revoked = storage.revoke_user_sessions(user.id)
        start_session(
            request=request,
            response=response,
            storage=storage,
            settings=settings,
            user_id=user.id,
        )
    log_event(logger, "password_changed", user_id=user.id, revoked_sessions=revoked)
    return {"user": user_payload(user)}


@router.post("/password-reset/request")
def request_password_reset_endpoint(
    payload: PasswordResetRequest,
    background: BackgroundTasks,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
    health: EmailHealth = Depends(get_email_health),
    metrics: AppMetrics = Depends(get_metrics),
) -> dict:
    """Always answers `{"accepted": true}`: the response, and how long it takes,
    must not depend on whether the account exists or the email went out.
    Delivery runs after the response; a failure shows up as an error log, the
    `arranger_email_send_total{outcome="failed"}` counter and `/ready`."""
    user = storage.get_user_with_password(payload.email)
    result: dict = {"accepted": True}
    log_event(
        logger,
        "password_reset_request",
        user_found=user is not None,
        email_domain=email_domain(payload.email),
        provider=settings.email_provider,
    )
    if user is None:
        return result
    token = new_token()
    storage.create_password_reset_token(
        user["id"], hash_token(token), settings.password_reset_minutes
    )
    reset_link = build_password_reset_link(settings, token)
    email = payload.email
    minutes = settings.password_reset_minutes
    background.add_task(
        deliver_email,
        "password_reset",
        lambda: email_sender.send_password_reset(email, reset_link, minutes),
        provider=settings.email_provider,
        recipient=email,
        health=health,
        metrics=metrics,
    )
    if settings.is_local:
        # Development convenience only: lets the local frontend finish the flow
        # without a mailbox. Never present outside APP_ENV=development|test.
        result["reset_token"] = token
        result["reset_link"] = reset_link
    return result


@router.post("/password-reset/confirm", response_model=UserResponse)
def confirm_password_reset_endpoint(
    payload: PasswordResetConfirmRequest,
    storage: Storage = Depends(get_storage),
    passwords: PasswordService = Depends(get_passwords),
    metrics: AppMetrics = Depends(get_metrics),
) -> dict:
    invalid = forbidden("Password reset token is invalid or expired.", "invalid_token")
    token_hash = hash_token(payload.token)
    pending = storage.get_password_reset_token(token_hash)
    if pending is None:
        metrics.auth_failures.inc(reason="invalid_reset_token")
        raise invalid
    owner = storage.get_user(pending["user_id"])
    if problems := password_problems(payload.password, owner["email"] if owner else None):
        raise _weak_password(problems)
    # Hash first (slow), then spend the token and change the password in one
    # transaction. The look-up above is advisory; the conditional UPDATE inside
    # consume_password_reset_token is what decides a race.
    user = storage.consume_password_reset_token(token_hash, passwords.hash(payload.password))
    if user is None:
        metrics.auth_failures.inc(reason="invalid_reset_token")
        raise invalid
    return {"user": user_payload(user)}


@router.post("/email/verify", response_model=UserResponse)
def verify_email_endpoint(
    payload: EmailVerifyRequest,
    storage: Storage = Depends(get_storage),
    metrics: AppMetrics = Depends(get_metrics),
) -> dict:
    user = storage.consume_email_verification_token(hash_token(payload.token))
    if user is None:
        metrics.auth_failures.inc(reason="invalid_verification_token")
        raise forbidden("Verification link is invalid or expired.", "invalid_token")
    return {"user": user_payload(user)}


@router.post("/email/resend")
def resend_verification_endpoint(
    background: BackgroundTasks,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
    health: EmailHealth = Depends(get_email_health),
    metrics: AppMetrics = Depends(get_metrics),
    limits: RateLimits = Depends(get_rate_limits),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    limits.email_resend.check(f"user:{user.id}")
    if user.email_verified:
        return {"accepted": True, "already_verified": True}
    _queue_verification_email(
        background=background,
        storage=storage,
        settings=settings,
        email_sender=email_sender,
        health=health,
        metrics=metrics,
        user_id=user.id,
        email=user.email,
    )
    return {"accepted": True, "already_verified": False}
