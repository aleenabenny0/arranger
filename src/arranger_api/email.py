"""Email delivery adapters for account workflows."""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

import httpx

from .settings import Settings

logger = logging.getLogger("arranger_api.email")
USER_AGENT = "arranger-api/0.1"


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


@dataclass(frozen=True)
class ConsoleEmailSender:
    def send_password_reset(self, email: str, reset_link: str, expires_minutes: int) -> None:
        logger.info(
            "password_reset_email",
            extra={
                "email": email,
                "reset_link": reset_link,
                "expires_minutes": expires_minutes,
                "provider": "console",
            },
        )


@dataclass(frozen=True)
class ResendEmailSender:
    api_key: str
    from_email: str
    subject: str

    def send_password_reset(self, email: str, reset_link: str, expires_minutes: int) -> None:
        if not self.api_key:
            raise RuntimeError("RESEND_API_KEY is required when EMAIL_PROVIDER=resend")

        text = (
            "Reset your Arranger password using this link:\n\n"
            f"{reset_link}\n\n"
            f"This link expires in {expires_minutes} minutes."
        )
        html = (
            "<p>Reset your Arranger password using the link below.</p>"
            f'<p><a href="{reset_link}">Reset password</a></p>'
            f"<p>This link expires in {expires_minutes} minutes.</p>"
        )
        payload = {
            "from": self.from_email,
            "to": [email],
            "subject": self.subject,
            "text": text,
            "html": html,
        }

        try:
            with httpx.Client(http2=True, timeout=10) as client:
                response = client.post(
                    "https://api.resend.com/emails",
                    content=json.dumps(payload),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                )
        except httpx.RequestError as exc:
            raise EmailSendError(f"Resend request failed: {exc}") from exc

        if response.status_code >= 300:
            raise EmailSendError(
                f"Resend returned status {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )


def build_password_reset_link(settings: Settings, token: str) -> str:
    query = urllib.parse.urlencode({"reset_token": token})
    return f"{settings.app_public_url}/?{query}"


def email_diagnostics(settings: Settings) -> dict:
    """Non-secret email configuration for startup logs and /diagnostics/email.

    Deliberately excludes resend_api_key itself - only whether it is set.
    """
    return {
        "provider": settings.email_provider,
        "has_resend_key": bool(settings.resend_api_key),
        "app_public_url": settings.app_public_url,
        "password_reset_from": settings.password_reset_from,
    }


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.email_provider == "resend":
        return ResendEmailSender(
            api_key=settings.resend_api_key,
            from_email=settings.password_reset_from,
            subject=settings.password_reset_subject,
        )
    if settings.email_provider in {"console", "none", ""}:
        return ConsoleEmailSender()
    raise RuntimeError(f"Unsupported EMAIL_PROVIDER: {settings.email_provider}")
