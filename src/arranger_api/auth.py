"""Cookie-session authentication for the API layer."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Response

SESSION_COOKIE = "arranger_session"
CSRF_COOKIE = "arranger_csrf"
CSRF_HEADER = "x-csrf-token"

PASSWORD_MIN_CHARS = 10
PASSWORD_MAX_BYTES = 256
LEGACY_PBKDF2_PREFIX = "pbkdf2_sha256$"
MAX_LEGACY_PBKDF2_ROUNDS = 5_000_000

# The most common passwords that are long enough to pass the length rule.
# Shorter ones are already rejected by PASSWORD_MIN_CHARS. Compared casefolded.
COMMON_PASSWORDS = frozenset(
    """
    1234567890 12345678910 123456789a 1234567891 0123456789 0987654321 9876543210
    1234554321 1122334455 123123123123 123456123456 123456789012 12345678901
    1234567890a 1234567890q 987654321a 1212121212 1q2w3e4r5t 1q2w3e4r5t6y
    1qaz2wsx3edc 1qazxsw23edc qazwsxedc1 qazwsxedcrfv zaq12wsx34 qwertyuiop
    qwerty1234 qwerty12345 qwerty123456 qwertyuiop123 qwertyqwerty asdfghjkl1
    asdfghjkl123 zxcvbnm123 password01 password11 password12 password99 password123
    password1234 password12345 password123456 password1234567890 passw0rd123
    p@ssw0rd123 p@ssword123 passwordpassword mypassword mypassword1 mypassword123
    iloveyou12 iloveyou123 iloveyou1234 letmein123 letmein1234 welcome123
    welcome1234 welcome12345 admin12345 admin123456 administrator adminadmin
    administrator1 changeme123 changeme1234 football123 baseball123 basketball
    basketball1 superman123 batman12345 dragon12345 monkey12345 sunshine123
    princess12 princess123 michael123 jennifer123 jessica123 charlie123 abc1234567
    abcd123456 abcdefghij abcdefghijk abcdefg123 a1b2c3d4e5 aa12345678 google12345
    liverpool1 liverpool123 manchester manchester1 chocolate1 chocolate123
    butterfly1 butterfly123 computer123 internet123 samsung123 starwars123
    pokemon123 minecraft123 whatever123 trustno1234 master12345 freedom123
    hello12345 helloworld helloworld1 helloworld123 summer2023 summer2024
    summer2025 summer2026 winter2023 winter2024 winter2025 winter2026 spring2024
    spring2025 spring2026 autumn2024 autumn2025 arranger123 arrangerarranger
    sheetmusic123 pianopiano pianopiano1 musicmusic
    """.split()
)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    display_name: str
    email_verified: bool = False
    session_id: str = ""


@dataclass(frozen=True)
class PasswordCheck:
    ok: bool
    needs_rehash: bool = False


def _verify_pbkdf2(password: str, stored: str) -> bool:
    try:
        scheme, rounds_text, salt, expected = stored.split("$", 3)
        rounds = int(rounds_text)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256" or not 1 <= rounds <= MAX_LEGACY_PBKDF2_ROUNDS:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds)
    return hmac.compare_digest(digest.hex(), expected)


def hash_password_pbkdf2(password: str, *, salt: str | None = None, rounds: int = 200_000) -> str:
    """The pre-Argon2 format. Kept only so tests can mint legacy hashes."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${digest.hex()}"


class PasswordService:
    """Argon2id hashing with a verify-and-upgrade path for legacy PBKDF2 hashes.

    Defaults follow the OWASP password storage cheat sheet for Argon2id
    (19 MiB memory, 2 iterations, 1 lane). They are constructor arguments so
    deployments can raise them and tests can lower them.
    """

    def __init__(
        self,
        *,
        time_cost: int = 2,
        memory_kib: int = 19_456,
        parallelism: int = 1,
    ) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        # Verified against when the account does not exist, so an unknown email
        # costs the same as a wrong password.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(24))

    @classmethod
    def from_settings(cls, settings) -> "PasswordService":
        return cls(
            time_cost=settings.argon2_time_cost,
            memory_kib=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, stored: str | None) -> PasswordCheck:
        """Check a password. `needs_rehash` is set when the stored hash should be
        replaced: any legacy PBKDF2 hash, or Argon2 with outdated parameters."""
        if len(password.encode("utf-8", "ignore")) > PASSWORD_MAX_BYTES * 4:
            self.verify_dummy("x")
            return PasswordCheck(False)
        if stored and stored.startswith("$argon2"):
            try:
                self._hasher.verify(stored, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return PasswordCheck(False)
            return PasswordCheck(True, self._hasher.check_needs_rehash(stored))
        if stored and stored.startswith(LEGACY_PBKDF2_PREFIX):
            return PasswordCheck(_verify_pbkdf2(password, stored), needs_rehash=True)
        # Unusable or unknown hash format: fail, at the usual cost.
        self.verify_dummy(password)
        return PasswordCheck(False)

    def verify_dummy(self, password: str) -> None:
        """Burn one verification so response time does not reveal account existence."""
        try:
            self._hasher.verify(self._dummy_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass


_default_passwords: PasswordService | None = None


def default_password_service() -> PasswordService:
    """Process-wide service built from the environment, for callers without an app."""
    global _default_passwords
    if _default_passwords is None:
        from .settings import load_settings

        _default_passwords = PasswordService.from_settings(load_settings())
    return _default_passwords


def hash_password(password: str) -> str:
    return default_password_service().hash(password)


def verify_password(password: str, stored: str) -> bool:
    return default_password_service().verify(password, stored).ok


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(presented: str | None, stored_hash: str | None) -> bool:
    """Constant-time check that `presented` hashes to `stored_hash`."""
    if not presented or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(presented), stored_hash)


def password_problems(password: str, email: str | None = None) -> list[str]:
    """Why a new password is unacceptable. Empty when it is fine.

    Length and known-bad checks only: composition rules ("one uppercase, one
    digit") push people towards predictable patterns and are not used.
    """
    problems = []
    if len(password) < PASSWORD_MIN_CHARS:
        problems.append(f"Password must be at least {PASSWORD_MIN_CHARS} characters.")
    if len(password.encode("utf-8", "ignore")) > PASSWORD_MAX_BYTES:
        problems.append(f"Password must be at most {PASSWORD_MAX_BYTES} bytes.")
    folded = password.casefold().strip()
    if folded in COMMON_PASSWORDS or (folded and len(set(folded)) == 1):
        problems.append("Password is too common. Choose something harder to guess.")
    if email and folded == email.casefold().strip():
        problems.append("Password must not be your email address.")
    return problems


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
        path="/",
    )


def set_csrf_cookie(
    response: Response,
    token: str,
    *,
    secure: bool = False,
    max_age: int = 60 * 60 * 24 * 30,
) -> None:
    # Readable by the single-page app on purpose: it copies the value into the
    # X-CSRF-Token header. The server trusts only the hash stored on the session.
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool = False) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=secure, samesite="lax", path="/")
    response.delete_cookie(CSRF_COOKIE, httponly=False, secure=secure, samesite="lax", path="/")


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": "unauthorized", "detail": "Sign in to access saved work."},
    )


def forbidden(detail: str, error: str = "forbidden") -> HTTPException:
    return HTTPException(status_code=403, detail={"error": error, "detail": detail})
