"""API error helpers.

Two rules:

- 4xx responses may explain themselves, but only with text that came from
  domain validation. `safe_message` refuses anything that looks like SQL, a
  file path or a traceback, whatever exception it arrived in.
- 5xx responses never explain themselves. The public body is a stable error
  code, a generic sentence and the request id; the real exception goes to the
  log under the same request id.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from arranger.render import RenderError

from .observability import current_request_id, get_logger

logger = get_logger("arranger_api.errors")

GENERIC_INVALID_INPUT = "The request could not be processed as sent."
GENERIC_SERVER_MESSAGES = {
    500: "Internal server error.",
    501: "Not implemented.",
    502: "Upstream service error.",
    503: "Service temporarily unavailable.",
    504: "Upstream service timed out.",
}
MAX_PUBLIC_MESSAGE_CHARS = 300
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

_UNSAFE_MESSAGE = re.compile(
    r"""
      [A-Za-z]:[\\/]                                  # C:\... drive paths
    | \\\\[\w.$-]+\\                                  # \\server\share
    | (?:^|[\s'"(=])/(?:[\w.@-]+/)+[\w.@-]*           # /unix/style/paths
    | \.py\b | \bTraceback\b | \bFile\s+" | \bline\s+\d+,\s+in\b
    | \b(?:SELECT|INSERT\s+INTO|DELETE\s+FROM|DROP\s+TABLE|CREATE\s+TABLE|ALTER\s+TABLE|PRAGMA)\b
    | \bUPDATE\s+\w+\s+SET\b
    | \bsqlite3?\b | \bpsycopg | \bOperationalError\b | \bIntegrityError\b
    | syntax\s+error\s+at\s+or\s+near | no\s+such\s+(?:table|column)
    | \bUNIQUE\s+constraint\b | violates\s+[\w\s-]*constraint | \brelation\s+"
    """,
    re.IGNORECASE | re.VERBOSE,
)


def safe_message(text: object, fallback: str = GENERIC_INVALID_INPUT) -> str:
    """`text` if it is fit to show a client, else `fallback`."""
    message = " ".join(str(text).split())
    if not message or _UNSAFE_MESSAGE.search(message):
        return fallback
    if len(message) > MAX_PUBLIC_MESSAGE_CHARS:
        message = message[: MAX_PUBLIC_MESSAGE_CHARS - 1].rstrip() + "…"
    return message


def api_error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """The one way to raise a public error: `raise api_error(409, "code", "Sentence.")`.

    `code` is stable API for clients to branch on; `message` is for people.
    For 5xx statuses the message is discarded and replaced by a generic one.
    """
    if not _ERROR_CODE.fullmatch(code):
        raise ValueError(f"error code must be snake_case: {code!r}")
    if status_code >= 500:
        message = GENERIC_SERVER_MESSAGES.get(status_code, GENERIC_SERVER_MESSAGES[500])
    return HTTPException(
        status_code=status_code,
        detail={"error": code, "detail": safe_message(message)},
        headers=headers,
    )


def bad_request(error: str, detail: str) -> HTTPException:
    return api_error(400, error, detail)


def conflict(error: str, detail: str) -> HTTPException:
    return api_error(409, error, detail)


def not_found(resource: str, record_id: str) -> HTTPException:
    shown = record_id if re.fullmatch(r"[\w .:-]{1,64}", record_id or "") else "requested"
    return HTTPException(
        status_code=404,
        detail={"error": "not_found", "detail": f"{resource} '{shown}' was not found"},
    )


def service_unavailable(code: str = "service_unavailable", retry_after: int = 5) -> HTTPException:
    return api_error(503, code, "", headers={"Retry-After": str(retry_after)})


# Status by stable error code. Anything not listed is a 400.
_IMPORT_ERROR_STATUS = {
    "limit_exceeded": 413, "unsupported_format": 415, "unsupported_midi": 415,
    "engraver_unavailable": 501, "audio_unavailable": 501,
}


def domain_error(exc: Exception) -> HTTPException:
    """Translate an exception from domain code into a public HTTP error.

    Known validation failures keep their (sanitised) message as a 4xx.
    Everything else is logged with the request id and becomes a generic 500.
    """
    if isinstance(exc, HTTPException):
        return exc
    # arranger.limits.ScoreImportError: carries a stable code and a vetted message.
    public = getattr(exc, "public", None)
    code = getattr(exc, "code", None)
    if isinstance(exc, ValueError) and isinstance(public, str) and isinstance(code, str):
        safe_code = code if _ERROR_CODE.fullmatch(code) else "import_failed"
        return api_error(_IMPORT_ERROR_STATUS.get(safe_code, 400), safe_code, public)
    if type(exc).__name__ == "ValidationError" and hasattr(exc, "errors"):
        # pydantic's text echoes input values; never forward it.
        return api_error(422, "invalid_input", "Stored or derived data failed validation.")
    if isinstance(exc, RenderError):
        return api_error(400, "invalid_plan", safe_message(exc, "The plan could not be rendered."))
    if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError):
        return api_error(400, "invalid_input", safe_message(exc))
    logger.error(
        "unhandled_domain_error",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"error_type": type(exc).__name__},
    )
    return api_error(500, "internal_error", "")


def public_error_body(status_code: int, detail: Any, request_id: str | None = None) -> dict:
    """The JSON body for an HTTP error. 5xx bodies are rebuilt from scratch."""
    rid = request_id or current_request_id()
    if status_code < 500:
        return {"detail": detail}
    code = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
        code = "internal_error"
    return {
        "detail": {
            "error": code,
            "detail": GENERIC_SERVER_MESSAGES.get(status_code, GENERIC_SERVER_MESSAGES[500]),
            "request_id": rid,
        }
    }
