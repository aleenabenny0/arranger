"""Email delivery adapters for account workflows."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .settings import Settings

logger = logging.getLogger("arranger_api.email")
USER_AGENT = "arranger-api/0.1"


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
        payload = json.dumps(
            {
                "from": self.from_email,
                "to": [email],
                "subject": self.subject,
                "text": text,
                "html": html,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status >= 300:
                    raise RuntimeError(f"Resend returned status {response.status}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Resend returned status {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Resend request failed: {exc.reason}") from exc


def build_password_reset_link(settings: Settings, token: str) -> str:
    query = urllib.parse.urlencode({"reset_token": token})
    return f"{settings.app_public_url}/?{query}"


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
