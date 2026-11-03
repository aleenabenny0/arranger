"""Email delivery adapters for account workflows.

Links sent by email point at the single-page app and carry their token in the
URL *fragment* (`/#/reset-password?token=...`). Browsers never send fragments
to a server, so the token stays out of access logs, proxy logs and Referer
headers. The app reads it from `location.hash` and POSTs it in a JSON body.
"""

from __future__ import annotations

import html
import logging
import sys
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import resend
from resend.exceptions import ResendError

from .observability import get_logger, log_event
from .settings import CONSOLE_EMAIL_PROVIDERS, LOCAL_ENVIRONMENTS, ConfigurationError, Settings

logger = get_logger("arranger_api.email")


class EmailSendError(RuntimeError):
    """Raised when an email provider rejects or fails to deliver a message.

    Carries the provider's raw status code and response body (never the
    request payload, which holds the reset link/token) so callers can log
    enough detail to diagnose delivery failures without re-deriving them.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class EmailSender(Protocol):
    def send_password_reset(self, email: str, reset_link: str, expires_minutes: int) -> None:
        """Send a password reset message."""

    def send_email_verification(self, email: str, verify_link: str, expires_minutes: int) -> None:
        """Send an address verification message."""


@dataclass(frozen=True)
class ConsoleEmailSender:
    """Development stand-in: prints the message to stderr instead of sending it.

    Usable only when `APP_ENV` is `development` or `test`. The link is written
    straight to stderr - never through the structured logger - so it cannot end
    up in shipped logs. Anywhere else, sending raises.
    """

    app_env: str = "development"

    def _emit(self, kind: str, email: str, link: str, expires_minutes: int) -> None:
        if self.app_env not in LOCAL_ENVIRONMENTS:
            raise EmailSendError(
                f"The console email sender is disabled when APP_ENV={self.app_env}.",
            )
        sys.stderr.write(
            f"[console email] {kind} for {email} (expires in {expires_minutes} min): {link}\n"
        )

    def send_password_reset(self, email: str, reset_link: str, expires_minutes: int) -> None:
        self._emit("password reset", email, reset_link, expires_minutes)

    def send_email_verification(self, email: str, verify_link: str, expires_minutes: int) -> None:
        self._emit("email verification", email, verify_link, expires_minutes)


@dataclass(frozen=True)
class ResendEmailSender:
    api_key: str
    from_email: str
    subject: str
    verification_subject: str = "Verify your Arranger email address"

    def _send(self, email: str, subject: str, text: str, html_body: str) -> None:
        if not self.api_key:
            raise EmailSendError("RESEND_API_KEY is required when EMAIL_PROVIDER=resend")
        payload = {
            "from": self.from_email,
            "to": [email],
            "subject": subject,
            "text": text,
            "html": html_body,
        }
        try:
            resend.api_key = self.api_key
            resend.Emails.send(payload)
        except ResendError as exc:
            status_code = getattr(exc, "status_code", None)
            body = getattr(exc, "message", None) or str(exc)
            raise EmailSendError(
                "Resend rejected the email request",
                status_code=status_code,
                body=body,
            ) from exc
        except Exception as exc:
            raise EmailSendError("Resend request failed", body=str(exc)) from exc

    def send_password_reset(self, email: str, reset_link: str, expires_minutes: int) -> None:
        href = html.escape(reset_link, quote=True)
        self._send(
            email,
            self.subject,
            "Reset your Arranger password using this link:\n\n"
            f"{reset_link}\n\n"
            f"This link expires in {expires_minutes} minutes. "
            "If you did not ask for it, you can ignore this email.",
            "<p>Reset your Arranger password using the link below.</p>"
            f'<p><a href="{href}">Reset password</a></p>'
            f"<p>This link expires in {expires_minutes} minutes. "
            "If you did not ask for it, you can ignore this email.</p>",
        )

    def send_email_verification(self, email: str, verify_link: str, expires_minutes: int) -> None:
        href = html.escape(verify_link, quote=True)
        self._send(
            email,
            self.verification_subject,
            "Confirm your email address for Arranger using this link:\n\n"
            f"{verify_link}\n\n"
            f"This link expires in {expires_minutes} minutes.",
            "<p>Confirm your email address for Arranger using the link below.</p>"
            f'<p><a href="{href}">Verify email address</a></p>'
            f"<p>This link expires in {expires_minutes} minutes.</p>",
        )


def _fragment_link(settings: Settings, route: str, token: str) -> str:
    return f"{settings.public_base_url}/#/{route}?token={urllib.parse.quote(token, safe='')}"


def build_password_reset_link(settings: Settings, token: str) -> str:
    return _fragment_link(settings, "reset-password", token)


def build_email_verification_link(settings: Settings, token: str) -> str:
    return _fragment_link(settings, "verify-email", token)


def email_diagnostics(settings: Settings) -> dict:
    """Non-secret email configuration for startup logs and /diagnostics/email.

    Deliberately excludes resend_api_key itself - only whether it is set.
    """
    return {
        "provider": settings.email_provider,
        "has_resend_key": bool(settings.resend_api_key),
        "app_public_url": settings.public_base_url,
        "password_reset_from": settings.password_reset_from,
    }


def build_email_sender(settings: Settings) -> EmailSender:
    """Build the configured sender. Never substitutes a different provider.

    A misconfiguration raises `ConfigurationError` so the process fails at
    startup instead of quietly printing reset links to a console nobody reads.
    """
    provider = settings.email_provider
    if provider == "resend":
        if settings.is_production and not settings.resend_api_key:
            raise ConfigurationError(["RESEND_API_KEY is required when EMAIL_PROVIDER=resend."])
        return ResendEmailSender(
            api_key=settings.resend_api_key,
            from_email=settings.password_reset_from,
            subject=settings.password_reset_subject,
            verification_subject=settings.email_verification_subject,
        )
    if provider in CONSOLE_EMAIL_PROVIDERS:
        if not settings.is_local:
            raise ConfigurationError(
                [f"EMAIL_PROVIDER=console is not allowed when APP_ENV={settings.app_env}."]
            )
        return ConsoleEmailSender(app_env=settings.app_env)
    raise ConfigurationError([f"EMAIL_PROVIDER '{provider}' is not supported."])


class EmailHealth:
    """Remembers recent send outcomes so `/ready` can report `email: ok|degraded`.

    Degraded means the most recent send failed, or at least half of the last
    `window` sends did. No sends yet counts as ok. Per process, like metrics.
    """

    def __init__(self, window: int = 10) -> None:
        self._outcomes: deque[tuple[bool, float]] = deque(maxlen=max(1, window))
        self._lock = threading.Lock()

    def record(self, ok: bool) -> None:
        with self._lock:
            self._outcomes.append((bool(ok), time.time()))

    def status(self) -> str:
        with self._lock:
            outcomes = [ok for ok, _ in self._outcomes]
        if not outcomes:
            return "ok"
        failures = outcomes.count(False)
        if not outcomes[-1] or failures * 2 >= len(outcomes):
            return "degraded"
        return "ok"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            outcomes = list(self._outcomes)
        return {
            "status": self.status(),
            "recent_sends": len(outcomes),
            "recent_failures": sum(1 for ok, _ in outcomes if not ok),
        }


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def deliver_email(
    kind: str,
    send: Callable[[], None],
    *,
    provider: str,
    recipient: str,
    health: EmailHealth | None = None,
    metrics: Any | None = None,
) -> bool:
    """Send one message and account for the outcome. Never raises.

    Callers must not change their public response based on the result (that
    would leak whether an account exists); the failure is visible as an error
    log, the `arranger_email_send_total{outcome="failed"}` counter, and
    `email: degraded` on `/ready`.
    """
    try:
        send()
    except Exception as exc:
        body = getattr(exc, "body", None)
        log_event(
            logger,
            f"{kind}_email_failed",
            level=logging.ERROR,
            provider=provider,
            email_domain=email_domain(recipient),
            error_type=type(exc).__name__,
            status_code=getattr(exc, "status_code", None),
            response_body=body[:500] if isinstance(body, str) else None,
        )
        if health is not None:
            health.record(False)
        if metrics is not None:
            metrics.email_sends.inc(kind=kind, outcome="failed")
        return False
    log_event(
        logger,
        f"{kind}_email_sent",
        provider=provider,
        email_domain=email_domain(recipient),
    )
    if health is not None:
        health.record(True)
    if metrics is not None:
        metrics.email_sends.inc(kind=kind, outcome="sent")
    return True
