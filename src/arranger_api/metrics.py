"""In-process metrics registry with Prometheus text exposition.

Deliberately dependency-free: a few counters and histograms do not justify a
client library. Every registry is independent, so each `create_app()` call
(and therefore each test) gets its own numbers.

Values are per process. When the API runs with several workers, scrape each
one or aggregate in the collector; nothing here is shared between processes.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable

DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

# A label set is created per distinct combination of values. Route templates,
# status classes and fixed reason codes keep that small; the cap is a backstop
# against a caller accidentally labelling by something unbounded.
MAX_SERIES_PER_METRIC = 2_000
OVERFLOW_LABEL = "overflow"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _format_value(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


class _Metric:
    kind = "untyped"

    def __init__(self, name: str, help_text: str, labelnames: Iterable[str] = ()):
        self.name = name
        self.help_text = help_text
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, object], existing: dict) -> tuple[str, ...]:
        unknown = set(labels) - set(self.labelnames)
        if unknown:
            raise ValueError(f"unknown labels for {self.name}: {sorted(unknown)}")
        key = tuple(str(labels.get(name, "")) for name in self.labelnames)
        if key not in existing and len(existing) >= MAX_SERIES_PER_METRIC:
            return tuple(OVERFLOW_LABEL for _ in self.labelnames)
        return key

    def _label_text(self, key: tuple[str, ...], extra: str = "") -> str:
        parts = [f'{name}="{_escape_label(value)}"' for name, value in zip(self.labelnames, key, strict=True)]
        if extra:
            parts.append(extra)
        return "{" + ",".join(parts) + "}" if parts else ""

    def header(self) -> list[str]:
        return [
            f"# HELP {self.name} {_escape_help(self.help_text)}",
            f"# TYPE {self.name} {self.kind}",
        ]

    def render(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_text: str, labelnames: Iterable[str] = ()):
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: object) -> None:
        if amount < 0:
            raise ValueError("counters only go up")
        with self._lock:
            key = self._key(labels, self._values)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: object) -> float:
        with self._lock:
            return self._values.get(tuple(str(labels.get(n, "")) for n in self.labelnames), 0.0)

    def total(self) -> float:
        with self._lock:
            return sum(self._values.values())

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        lines = self.header()
        if not items and not self.labelnames:
            lines.append(f"{self.name} 0")
        for key, value in items:
            lines.append(f"{self.name}{self._label_text(key)} {_format_value(value)}")
        return lines


class Histogram(_Metric):
    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Iterable[str] = (),
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ):
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(float(b) for b in buckets))
        # key -> [per-bucket counts..., sum, count]
        self._values: dict[tuple[str, ...], list[float]] = {}

    def observe(self, value: float, **labels: object) -> None:
        with self._lock:
            key = self._key(labels, self._values)
            series = self._values.get(key)
            if series is None:
                series = [0.0] * (len(self.buckets) + 2)
                self._values[key] = series
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    series[index] += 1
            series[-2] += value
            series[-1] += 1

    def count(self, **labels: object) -> float:
        with self._lock:
            series = self._values.get(tuple(str(labels.get(n, "")) for n in self.labelnames))
            return series[-1] if series else 0.0

    def render(self) -> list[str]:
        with self._lock:
            items = sorted((key, list(series)) for key, series in self._values.items())
        lines = self.header()
        for key, series in items:
            for index, bound in enumerate(self.buckets):
                le = f'le="{_format_value(bound)}"'
                lines.append(
                    f"{self.name}_bucket{self._label_text(key, le)} {_format_value(series[index])}"
                )
            inf_label = self._label_text(key, 'le="+Inf"')
            lines.append(f"{self.name}_bucket{inf_label} {_format_value(series[-1])}")
            lines.append(f"{self.name}_sum{self._label_text(key)} {_format_value(series[-2])}")
            lines.append(f"{self.name}_count{self._label_text(key)} {_format_value(series[-1])}")
        return lines


class MetricsRegistry:
    """Named counters and histograms, rendered as Prometheus text format 0.0.4."""

    content_type = "text/plain; version=0.0.4; charset=utf-8"

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str = "", labelnames: Iterable[str] = ()) -> Counter:
        return self._get_or_create(Counter, name, help_text, labelnames)

    def histogram(
        self,
        name: str,
        help_text: str = "",
        labelnames: Iterable[str] = (),
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        return self._get_or_create(Histogram, name, help_text, labelnames, buckets=buckets)

    def _get_or_create(self, cls, name, help_text, labelnames, **kwargs):
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = cls(name, help_text or name, labelnames, **kwargs)
                self._metrics[name] = metric
            elif not isinstance(metric, cls) or metric.labelnames != tuple(labelnames):
                raise ValueError(f"metric {name} is already registered with a different shape")
            return metric

    def render(self) -> str:
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics)]
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


class AppMetrics:
    """The metrics this API records, created once per application."""

    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        self.registry = registry or MetricsRegistry()
        self.http_requests = self.registry.counter(
            "arranger_http_requests_total",
            "HTTP requests by method, route template and status class.",
            ("method", "route", "status_class"),
        )
        self.http_duration = self.registry.histogram(
            "arranger_http_request_duration_seconds",
            "HTTP request duration in seconds by route template.",
            ("route",),
        )
        self.auth_failures = self.registry.counter(
            "arranger_auth_failures_total",
            "Rejected authentication and authorisation attempts by reason.",
            ("reason",),
        )
        self.rate_limit_hits = self.registry.counter(
            "arranger_rate_limit_hits_total",
            "Requests rejected by a rate limiter.",
            ("limiter",),
        )
        self.rate_limit_store_errors = self.registry.counter(
            "arranger_rate_limit_store_errors_total",
            "Rate-limit store failures that fell back to the in-memory store.",
        )
        self.email_sends = self.registry.counter(
            "arranger_email_send_total",
            "Outbound email attempts by kind and outcome.",
            ("kind", "outcome"),
        )
        self.request_rejections = self.registry.counter(
            "arranger_request_rejections_total",
            "Requests rejected before routing, by reason.",
            ("reason",),
        )

        self.jobs_finished = self.registry.counter(
            "arranger_jobs_finished_total",
            "Background jobs by kind and final status.",
            ("kind", "status"),
        )

    def render(self) -> str:
        return self.registry.render()
