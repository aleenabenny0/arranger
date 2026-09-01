"""Production logging helpers."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from fastapi import Request


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str, sort_keys=True))


def request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def monotonic_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
