"""Structured logging, secret redaction and request ids.

`configure_logging` owns exactly one logger, `arranger_api`. It never touches
the root logger: `logging.basicConfig` is a no-op whenever something else
(pytest, uvicorn --log-config, a host framework) already installed root
handlers, which used to leave this logger at the inherited WARNING level and
silently drop every structured event.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

LOGGER_NAME = "arranger_api"
REDACTED = "[REDACTED]"

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
request_id_var: ContextVar[str] = ContextVar("arranger_request_id", default="")

# Keys whose values never belong in a log, wherever they appear in a payload.
SENSITIVE_KEY_PATTERN = re.compile(
    r"token|password|passwd|secret|authorization|cookie|reset_link|verify_link|api_?key",
    re.IGNORECASE,
)
# Configuration names that match the pattern above but hold nothing secret.
NON_SECRET_KEYS = frozenset(
    {
        "password_reset_from",
        "password_reset_subject",
        "password_reset_minutes",
        "email_verification_tokens",
        "password_reset_tokens",
    }
)


def is_sensitive_key(key: object) -> bool:
    return (
        isinstance(key, str)
        and key not in NON_SECRET_KEYS
        and SENSITIVE_KEY_PATTERN.search(key) is not None
    )


# Secrets that turn up inside free text: query strings, headers, provider keys.
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b((?:reset_|verify_|csrf_|access_|refresh_)?token|password|secret|api_?key)"
            r"(\s*[=:]\s*)([^\s&\"',;]+)"
        ),
        r"\1\2" + REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 " + REDACTED),
    (re.compile(r"\bre_[A-Za-z0-9_]{8,}\b"), REDACTED),
)

_STANDARD_RECORD_KEYS = frozenset(
    logging.LogRecord("x", logging.INFO, "x", 0, "", (), None).__dict__
) | {"message", "asctime", "request_id", "taskName"}


def scrub_text(text: str) -> str:
    """Mask secrets embedded in free text (URLs, header dumps, provider keys)."""
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a copy of `value` with sensitive keys masked at any depth."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if is_sensitive_key(key) and item is not None and not isinstance(item, bool)
                else redact(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def _redact_message(message: str) -> str:
    stripped = message.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            return scrub_text(message)
        if isinstance(payload, dict):
            return json.dumps(redact(payload), default=str, sort_keys=True)
    return scrub_text(message)


class RedactionFilter(logging.Filter):
    """Mask secrets in a record before any handler sees it, and stamp the request id.

    Attached to the `arranger_api` logger, to each of its child loggers and to
    the handler, so records are clean for every handler that receives them,
    including ones this module did not install (pytest's caplog, a host's).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_arranger_redacted", False):
            return True
        if not getattr(record, "request_id", ""):
            record.request_id = request_id_var.get()
        if isinstance(record.msg, str):
            record.msg = _redact_message(record.msg)
        if isinstance(record.args, dict):
            record.args = redact(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(
                redact(arg) if isinstance(arg, (dict, list, tuple, str)) else arg
                for arg in record.args
            )
        for key, item in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_KEYS or key.startswith("_"):
                continue
            if is_sensitive_key(key) and item is not None and not isinstance(item, bool):
                record.__dict__[key] = REDACTED
            else:
                record.__dict__[key] = redact(item)
        record._arranger_redacted = True
        return True


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line. Structured events are merged, not nested."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
        }
        rid = getattr(record, "request_id", "") or request_id_var.get()
        if rid:
            payload["request_id"] = rid
        message = record.getMessage()
        merged = False
        if message.lstrip().startswith("{"):
            try:
                event = json.loads(message)
            except ValueError:
                event = None
            if isinstance(event, dict):
                payload.update(event)
                merged = True
        if not merged:
            payload["message"] = message
        for key, item in record.__dict__.items():
            if key in _STANDARD_RECORD_KEYS or key.startswith("_") or key in payload:
                continue
            payload[key] = item
        if record.exc_info:
            payload["exception"] = scrub_text(self.formatException(record.exc_info))
        return json.dumps(redact(payload), default=str, sort_keys=True)


class _StderrHandler(logging.StreamHandler):
    """Stream handler that resolves `sys.stderr` at emit time.

    Test runners and process supervisors replace `sys.stderr`; holding the
    stream captured at configuration time ends in writes to a closed file.
    """

    _arranger_api_handler = True

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:  # pragma: no cover - StreamHandler API
        pass


_REDACTION_FILTER = RedactionFilter()


def _ensure_filter(logger: logging.Logger) -> None:
    if _REDACTION_FILTER not in logger.filters:
        logger.addFilter(_REDACTION_FILTER)


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """Return an `arranger_api` logger with the redaction filter attached.

    Logger-level filters do not run for records that arrive from child
    loggers, so every child gets the filter itself.
    """
    if name != LOGGER_NAME and not name.startswith(LOGGER_NAME + "."):
        name = f"{LOGGER_NAME}.{name}"
    logger = logging.getLogger(name)
    _ensure_filter(logger)
    return logger


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure the `arranger_api` logger explicitly. Safe to call repeatedly.

    `propagate` stays True on purpose: pytest's `caplog` and any host-installed
    root handler keep receiving these records (already redacted by the logger
    filter). A bare process has no root handlers, so nothing is duplicated.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(logger.handlers):
        if getattr(handler, "_arranger_api_handler", False):
            logger.removeHandler(handler)
    handler = _StderrHandler()
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(_REDACTION_FILTER)
    logger.addHandler(handler)
    logger.propagate = True
    _ensure_filter(logger)
    for name, child in list(logging.root.manager.loggerDict.items()):
        if name.startswith(LOGGER_NAME + ".") and isinstance(child, logging.Logger):
            _ensure_filter(child)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logger


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event. The message itself is the JSON document."""
    payload: dict[str, Any] = {"event": event, **fields}
    rid = request_id_var.get()
    if rid and "request_id" not in payload:
        payload["request_id"] = rid
    logger.log(level, json.dumps(redact(payload), default=str, sort_keys=True))


def resolve_request_id(inbound: str | None) -> str:
    """Accept a caller-supplied id only when it is short and inert."""
    if inbound and REQUEST_ID_PATTERN.fullmatch(inbound):
        return inbound
    return str(uuid.uuid4())


def current_request_id() -> str:
    return request_id_var.get()


def request_id(request: Any) -> str:
    """The id assigned to this request by the request-context middleware."""
    assigned = getattr(getattr(request, "state", None), "request_id", "")
    if assigned:
        return assigned
    return request_id_var.get() or resolve_request_id(request.headers.get("x-request-id"))


def monotonic_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
