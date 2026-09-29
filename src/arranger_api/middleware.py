"""Pure ASGI middleware: request context, request guard, body-size limit.

Written against the ASGI interface rather than `BaseHTTPMiddleware` so they
see the raw `receive` stream (needed to count body bytes), keep context
variables intact for the endpoint, and do not buffer responses.

Stack order, outermost first (see `create_app`):

    RequestContextMiddleware   request id, access log, metrics, response headers,
                               last-resort 500
    CORSMiddleware
    RequestGuardMiddleware     Origin allow-list, CSRF pre-check, upload flood guard
    BodyLimitMiddleware        Content-Length check + streamed byte count
    router
"""

from __future__ import annotations

import hmac
import re
import time
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from .errors import public_error_body
from .observability import get_logger, log_event, monotonic_ms, request_id_var, resolve_request_id
from .security import UNSAFE_METHODS, client_ip, origin_problem

logger = get_logger("arranger_api")

# Unauthenticated entry points: there is no session to bind a CSRF token to.
# They are still covered by the Origin/Referer allow-list below.
CSRF_EXEMPT_PATHS = frozenset(
    {
        "/auth/register",
        "/auth/login",
        "/auth/password-reset/request",
        "/auth/password-reset/confirm",
        "/auth/email/verify",
    }
)

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        # 'wasm-unsafe-eval' lets the same-origin WebAssembly notation renderer
        # compile. It does not permit eval() or inline script.
        "script-src 'self' 'wasm-unsafe-eval'",
        # No inline styles either: the stylesheet is a file, and the notation
        # SVG uses presentation attributes, which CSP does not govern.
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "media-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    # no-referrer, not same-origin: reset and verification links carry tokens.
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # With the opener policy above this isolates the page from cross-origin
    # windows and resources (Spectre). Every asset the app loads is same-origin.
    "Cross-Origin-Embedder-Policy": "require-corp",
}
HSTS_VALUE = "max-age=31536000; includeSubDomains"
# Third-party code under /vendor/ is requested with its version in the URL, so it
# can be reused for a long time. The app's own modules are not fingerprinted:
# they are revalidated on every load, so a deploy never leaves a browser running
# a new module against an old one.
VENDOR_CACHE_CONTROL = "public, max-age=604800"
_DIGITS = re.compile(r"^[0-9]{1,18}$")


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = public_error_body(status_code, {"error": code, "detail": message})
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _is_static(scope: Scope) -> bool:
    return isinstance(scope.get("endpoint"), StaticFiles)


def route_label(scope: Scope) -> str:
    """Low-cardinality label for metrics and logs: the template, never the raw path."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    return "static" if _is_static(scope) else "unmatched"


class RequestContextMiddleware:
    """Request id, access log, metrics and response headers for every response."""

    def __init__(self, app: ASGIApp, *, settings: Any, metrics: Any | None = None) -> None:
        self.app = app
        self.settings = settings
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = resolve_request_id(Headers(scope=scope).get("x-request-id"))
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id_var.set(rid)
        start = time.monotonic()
        status_holder = {"status": 500, "started": False}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                self._decorate(scope, message, rid)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            logger.error(
                "unhandled_exception",
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={"path": scope.get("path"), "method": scope.get("method")},
            )
            if status_holder["started"]:
                raise
            response = _error_response(500, "internal_server_error", "")
            await response(scope, receive, send_with_headers)
        finally:
            self._record(scope, status_holder["status"], start)
            request_id_var.reset(token)

    def _decorate(self, scope: Scope, message: Message, rid: str) -> None:
        headers = MutableHeaders(scope=message)
        headers["X-Request-ID"] = rid
        for key, value in SECURITY_HEADERS.items():
            if key not in headers:
                headers[key] = value
        if self.settings.is_production and "Strict-Transport-Security" not in headers:
            headers["Strict-Transport-Security"] = HSTS_VALUE

        status = message["status"]
        if _is_static(scope) and status in (200, 206, 304):
            # The frontend is public and identical for everyone, so it may be
            # stored; whether it may be reused without asking depends on what it is.
            if "Cache-Control" not in headers:
                is_vendor = str(scope.get("path", "")).startswith("/vendor/")
                headers["Cache-Control"] = VENDOR_CACHE_CONTROL if is_vendor else "no-cache"
            return
        # Everything else is API output: per-user, or about to be.
        headers["Cache-Control"] = "no-store"
        headers["Pragma"] = "no-cache"
        headers.add_vary_header("Cookie")

    def _record(self, scope: Scope, status: int, start: float) -> None:
        label = route_label(scope)
        method = scope.get("method", "")
        peer = scope_client_ip(scope, self.settings)
        log_event(
            logger,
            "http_request",
            method=method,
            path=scope.get("path"),
            route=label,
            status_code=status,
            duration_ms=monotonic_ms(start),
            client_ip=peer,
        )
        if self.metrics is not None:
            self.metrics.http_requests.inc(
                method=method, route=label, status_class=f"{status // 100}xx"
            )
            self.metrics.http_duration.observe(time.monotonic() - start, route=label)


class _ScopeRequest:
    """The slice of `Request` that `client_ip` needs, built from a raw scope."""

    def __init__(self, scope: Scope) -> None:
        self.headers = Headers(scope=scope)
        client = scope.get("client")
        self.client = _Peer(client[0]) if client else None


class _Peer:
    def __init__(self, host: str) -> None:
        self.host = host


def scope_client_ip(scope: Scope, settings: Any) -> str:
    return client_ip(_ScopeRequest(scope), settings.trusted_proxy_count)


class RequestGuardMiddleware:
    """Reject forged and abusive requests before routing and before the body is read.

    1. Origin allow-list for every unsafe method, authenticated or not. This is
       the CSRF defence for login, registration and the reset/verify endpoints,
       which have no session to bind a token to.
    2. CSRF pre-check: an unsafe request carrying a session cookie must carry
       `X-CSRF-Token` (and it must equal the CSRF cookie when that is present).
       The authoritative check - header hash equals the hash stored on the
       session - happens in `deps.get_current_session`, which has the database.
    3. Upload flood guard: a per-IP budget on the large-body path prefixes,
       enforced here so an over-budget client cannot make us read 40 MB first.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Any,
        metrics: Any | None = None,
        rate_limits: Any | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.metrics = metrics
        self.rate_limits = rate_limits
        self.allowed_origins = settings.allowed_origins

    def _reject(self, reason: str) -> None:
        if self.metrics is not None:
            self.metrics.request_rejections.inc(reason=reason)
            if reason in {"csrf", "origin"}:
                self.metrics.auth_failures.inc(reason=reason)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        path = scope["path"]
        cookies = _parse_cookies(headers.get("cookie", ""))

        if self.settings.csrf_protection:
            problem = origin_problem(headers, self.allowed_origins, has_cookies=bool(cookies))
            if problem is not None:
                self._reject("origin")
                message = (
                    "Requests that carry cookies must send an Origin header."
                    if problem == "origin_required"
                    else "This origin is not allowed to make this request."
                )
                await _error_response(403, problem, message)(scope, receive, send)
                return

            if cookies.get(SESSION_COOKIE) and path not in CSRF_EXEMPT_PATHS:
                header_token = headers.get(CSRF_HEADER)
                cookie_token = cookies.get(CSRF_COOKIE)
                mismatch = bool(cookie_token) and not hmac.compare_digest(
                    (header_token or "").encode(), cookie_token.encode()
                )
                if not header_token or mismatch:
                    self._reject("csrf")
                    await _error_response(403, "csrf_failed", "Missing or invalid CSRF token.")(
                        scope, receive, send
                    )
                    return

        if self.rate_limits is not None and is_upload_path(path, self.settings):
            key = f"ip:{scope_client_ip(scope, self.settings)}"
            decision = await run_in_threadpool(self.rate_limits.upload.hit, key)
            if not decision.allowed:
                self._reject("upload_rate_limit")
                await _error_response(
                    429,
                    "rate_limited",
                    "Too many requests. Try again shortly.",
                    headers={"Retry-After": str(decision.retry_after)},
                )(scope, receive, send)
                return

        await self.app(scope, receive, send)


def _parse_cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies.setdefault(name, value.strip().strip('"'))
    return cookies


def is_upload_path(path: str, settings: Any) -> bool:
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/")
        for prefix in settings.upload_path_prefixes
        if prefix
    )


def body_limit_for(path: str, settings: Any) -> int:
    """Large cap for the upload prefixes, the ordinary cap everywhere else."""
    return settings.max_upload_bytes if is_upload_path(path, settings) else settings.max_request_bytes


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Enforce the request body cap on the bytes actually received.

    `Content-Length` is only a claim: chunked requests do not send one and a
    client can lie. The header is still checked first because it rejects an
    honest oversized request without reading anything; the real enforcement
    counts bytes as they arrive and stops at the limit.
    """

    def __init__(self, app: ASGIApp, *, settings: Any, metrics: Any | None = None) -> None:
        self.app = app
        self.settings = settings
        self.metrics = metrics

    def _count(self, reason: str) -> None:
        if self.metrics is not None:
            self.metrics.request_rejections.inc(reason=reason)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = body_limit_for(scope["path"], self.settings)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            if not _DIGITS.fullmatch(declared.strip()):
                self._count("bad_content_length")
                response = _error_response(
                    400, "invalid_content_length", "Content-Length must be a non-negative integer."
                )
                await response(scope, receive, send)
                return
            if int(declared) > limit:
                self._count("body_too_large")
                await self._too_large()(scope, receive, send)
                return

        state = {"received": 0, "too_large": False, "started": False, "replaced": False}

        async def limited_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > limit:
                    state["too_large"] = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            if state["replaced"]:
                return  # the application's own response was superseded by the 413
            if state["too_large"] and not state["started"]:
                # The framework caught our exception and is answering with its
                # own error (FastAPI turns body-read failures into a 400).
                state["replaced"] = True
                self._count("body_too_large")
                await self._too_large()(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if state["started"] or state["replaced"]:
                return
            state["replaced"] = True
            self._count("body_too_large")
            await self._too_large()(scope, receive, send)

    @staticmethod
    def _too_large() -> JSONResponse:
        return _error_response(
            413,
            "request_too_large",
            "Request body is too large.",
            headers={"Connection": "close"},
        )
