"""Request hardening helpers for the API layer."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from threading import Lock

from fastapi import HTTPException, Request


@dataclass
class RateLimiter:
    requests: int
    window_seconds: int
    buckets: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    lock: Lock = field(default_factory=Lock)

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self.lock:
            bucket = self.buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.requests:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limited",
                        "detail": "Too many requests. Try again shortly.",
                    },
                )
            bucket.append(now)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
