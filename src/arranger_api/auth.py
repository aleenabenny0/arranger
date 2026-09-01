"""Cookie-session authentication for the API layer."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Response

SESSION_COOKIE = "arranger_session"


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    display_name: str


def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, expected = stored.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
    return hmac.compare_digest(digest.hex(), expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def set_session_cookie(
    response: Response,
    token: str,
    *,
    secure: bool = False,
    max_age: int = 60 * 60 * 24 * 30,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age,
    )


def clear_session_cookie(response: Response, *, secure: bool = False) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=secure, samesite="lax")


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": "unauthorized", "detail": "Sign in to access saved work."},
    )
