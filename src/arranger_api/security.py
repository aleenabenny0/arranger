"""Request hardening helpers: client address, rate limiting, origin checks."""

from __future__ import annotations

import hashlib
import ipaddress
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import HTTPException

from .settings import normalize_origin

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_RATE_LIMIT_KEY_CHARS = 200


# --- client address ---------------------------------------------------------


def _parse_ip(value: str | None) -> str | None:
    """Return a canonical IP string, or None when `value` is not an address."""
    if not value:
        return None
    candidate = value.strip().strip('"')
    if candidate.startswith("["):  # [v6]:port
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:  # v4:port
        candidate = candidate.split(":", 1)[0]
    candidate = candidate.split("%", 1)[0]  # zone id
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def client_ip(request: Any, trusted_proxy_count: int | None = None) -> str:
    """The address rate limits and session records are keyed on.

    `X-Forwarded-For` is client-controlled, so it is ignored unless the
    deployment states how many proxies it sits behind. With N trusted proxies
    the client is the Nth entry from the right: each trusted hop appends the
    peer it saw, and everything further left is whatever the client sent.
    Anything that does not parse as an IP falls back to the socket peer.
    """
    peer = request.client.host if getattr(request, "client", None) else None
    peer_ip = _parse_ip(peer) or (peer or "unknown")
    if trusted_proxy_count is None:
        settings = getattr(getattr(getattr(request, "app", None), "state", None), "settings", None)
        trusted_proxy_count = getattr(settings, "trusted_proxy_count", 0)
    if trusted_proxy_count <= 0:
        return peer_ip
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer_ip
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if len(hops) < trusted_proxy_count:
        return peer_ip
    return _parse_ip(hops[-trusted_proxy_count]) or peer_ip


def coarse_ip(value: str | None) -> str:
    """A network-level rendering of an address, for showing users their sessions."""
    parsed = _parse_ip(value)
    if parsed is None:
        return "unknown"
    address = ipaddress.ip_address(parsed)
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{parsed}/{prefix}", strict=False))


# --- origin allow-list ------------------------------------------------------


def origin_problem(
    headers: Any,
    allowed_origins: frozenset[str] | set[str],
    *,
    has_cookies: bool,
) -> str | None:
    """Why an unsafe request fails the Origin/Referer allow-list, or None.

    Browsers attach `Origin` to every cross-site request and to same-origin
    requests with unsafe methods, so a browser-driven forgery always carries
    one. A request with neither header is accepted only when it also carries
    no cookies: that is a non-browser client with no ambient authority.
    """
    origin = headers.get("origin")
    source = origin if origin is not None else headers.get("referer")
    if source is None:
        return "origin_required" if has_cookies else None
    if normalize_origin(source) in allowed_origins:
        return None
    return "origin_not_allowed"


# --- rate limiting ----------------------------------------------------------


@dataclass(frozen=True)
class RateLimitHit:
    count: int  # hits recorded in the current window, including this one
    reset_after: int  # whole seconds until the window closes


class RateLimitStore(Protocol):
    """Counts hits per key inside a fixed window."""

    def hit(self, key: str, window_seconds: int) -> RateLimitHit:
        """Record one hit and return the running count for the window."""

    def cleanup(self) -> int:
        """Drop state for closed windows. Returns the number of buckets removed."""


class InMemoryRateLimitStore:
    """Per-process fixed-window counters with idle eviction and a hard cap.

    The window for a key opens at its first hit. Closed windows are swept at
    most once per `sweep_interval`; if the bucket count still exceeds
    `max_buckets` the least recently used buckets go first, so memory stays
    bounded no matter how many distinct keys an attacker can mint.
    """

    def __init__(
        self,
        *,
        max_buckets: int = 50_000,
        sweep_interval: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_buckets = max(1, max_buckets)
        self.sweep_interval = sweep_interval
        self._clock = clock
        self._buckets: OrderedDict[str, list[float]] = OrderedDict()  # key -> [start, count, window]
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)

    def hit(self, key: str, window_seconds: int) -> RateLimitHit:
        now = self._clock()
        with self._lock:
            if now - self._last_sweep >= self.sweep_interval:
                self._sweep(now)
            bucket = self._buckets.get(key)
            if bucket is None or now >= bucket[0] + bucket[2]:
                bucket = [now, 0.0, float(window_seconds)]
                self._buckets[key] = bucket
            bucket[1] += 1
            self._buckets.move_to_end(key)
            if len(self._buckets) > self.max_buckets:
                self._sweep(now)
                while len(self._buckets) > self.max_buckets:
                    self._buckets.popitem(last=False)
            remaining = bucket[0] + bucket[2] - now
            return RateLimitHit(count=int(bucket[1]), reset_after=max(1, int(remaining + 0.999)))

    def cleanup(self) -> int:
        with self._lock:
            return self._sweep(self._clock())

    def clear(self) -> None:
        """Forget every bucket. For tests that share one application."""
        with self._lock:
            self._buckets.clear()

    def _sweep(self, now: float) -> int:
        expired = [key for key, b in self._buckets.items() if now >= b[0] + b[2]]
        for key in expired:
            del self._buckets[key]
        self._last_sweep = now
        return len(expired)


class FallbackRateLimitStore:
    """Use `primary`; if it fails, keep limiting with `fallback` and report it.

    A broken shared store must not take the API down, and must not switch rate
    limiting off either.
    """

    def __init__(
        self,
        primary: RateLimitStore,
        fallback: RateLimitStore,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.on_error = on_error

    def hit(self, key: str, window_seconds: int) -> RateLimitHit:
        try:
            return self.primary.hit(key, window_seconds)
        except Exception as exc:  # noqa: BLE001 - any store failure degrades, never raises
            if self.on_error is not None:
                self.on_error(exc)
            return self.fallback.hit(key, window_seconds)

    def cleanup(self) -> int:
        removed = self.fallback.cleanup()
        try:
            removed += self.primary.cleanup()
        except Exception as exc:  # noqa: BLE001
            if self.on_error is not None:
                self.on_error(exc)
        return removed


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def rate_limited(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"error": "rate_limited", "detail": "Too many requests. Try again shortly."},
        headers={"Retry-After": str(max(1, int(retry_after)))},
    )


@dataclass
class RateLimiter:
    """`requests` per `window_seconds` for each key, counted in `store`."""

    requests: int
    window_seconds: int
    store: RateLimitStore = field(default_factory=InMemoryRateLimitStore)
    name: str = "default"
    on_limited: Callable[[str], None] | None = None

    def _store_key(self, key: str) -> str:
        full = f"{self.name}:{key}"
        if len(full) <= MAX_RATE_LIMIT_KEY_CHARS:
            return full
        return f"{self.name}:sha256:{hashlib.sha256(full.encode()).hexdigest()}"

    def hit(self, key: str) -> RateLimitDecision:
        result = self.store.hit(self._store_key(key), self.window_seconds)
        allowed = result.count <= self.requests
        if not allowed and self.on_limited is not None:
            self.on_limited(self.name)
        return RateLimitDecision(
            allowed=allowed,
            limit=self.requests,
            remaining=max(0, self.requests - result.count),
            retry_after=result.reset_after,
        )

    def check(self, key: str) -> None:
        """Record a hit; raise 429 with `Retry-After` when over the limit."""
        decision = self.hit(key)
        if not decision.allowed:
            raise rate_limited(decision.retry_after)


class RateLimits:
    """The application's limiters, all counting in one shared store.

    Lives at `app.state.rate_limits`. Routers needing their own budget call
    `limiter(name, requests, window_seconds)`; limiters are cached by name.
    """

    def __init__(
        self,
        settings: Any,
        store: RateLimitStore,
        on_limited: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self._on_limited = on_limited
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()
        self.read = self.limiter(
            "read", settings.read_rate_limit_requests, settings.read_rate_limit_window_seconds
        )
        self.write = self.limiter(
            "write", settings.rate_limit_requests, settings.rate_limit_window_seconds
        )
        self.auth = self.limiter(
            "auth", settings.auth_rate_limit_requests, settings.auth_rate_limit_window_seconds
        )
        self.password_reset = self.limiter(
            "password_reset",
            settings.password_reset_rate_limit_requests,
            settings.password_reset_rate_limit_window_seconds,
        )
        self.email_resend = self.limiter(
            "email_resend",
            settings.email_resend_rate_limit_requests,
            settings.email_resend_rate_limit_window_seconds,
        )
        self.upload = self.limiter(
            "upload",
            settings.upload_rate_limit_requests,
            settings.upload_rate_limit_window_seconds,
        )

    def limiter(self, name: str, requests: int, window_seconds: int) -> RateLimiter:
        with self._lock:
            existing = self._limiters.get(name)
            if existing is None:
                existing = RateLimiter(
                    requests=requests,
                    window_seconds=window_seconds,
                    store=self.store,
                    name=name,
                    on_limited=self._on_limited,
                )
                self._limiters[name] = existing
            return existing

    def for_request(self, method: str, route_template: str) -> RateLimiter:
        """Pick the limiter for a request from its method and route template."""
        if route_template == "/auth/password-reset/request":
            return self.password_reset
        if method in {"GET", "HEAD", "OPTIONS"}:
            return self.read
        if route_template.startswith("/auth"):
            return self.auth
        return self.write
