"""Security helper tests: rate-limit stores, client address, origins, log redaction."""

import json
import logging
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import HTTPException  # noqa: E402

from arranger_api.observability import (  # noqa: E402
    REDACTED,
    JsonLogFormatter,
    RedactionFilter,
    configure_logging,
    get_logger,
    log_event,
    redact,
    scrub_text,
)
from arranger_api.security import (  # noqa: E402
    FallbackRateLimitStore,
    InMemoryRateLimitStore,
    RateLimiter,
    client_ip,
    coarse_ip,
    origin_problem,
)
from arranger_api.storage import Database  # noqa: E402
from arranger_api.storage.rate_limits import DatabaseRateLimitStore  # noqa: E402


def test_rate_limiter_rejects_over_limit():
    limiter = RateLimiter(requests=2, window_seconds=60)
    limiter.check("client")
    limiter.check("client")
    try:
        limiter.check("client")
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("expected rate limit exception")


def test_rate_limiter_sends_retry_after_and_keeps_keys_separate():
    now = [1000.0]
    limiter = RateLimiter(
        requests=1, window_seconds=60, store=InMemoryRateLimitStore(clock=lambda: now[0])
    )
    limiter.check("a")
    limiter.check("b")  # near miss: another key is unaffected
    now[0] += 20
    with pytest.raises(HTTPException) as excinfo:
        limiter.check("a")
    assert excinfo.value.headers == {"Retry-After": "40"}

    now[0] += 41  # window closed
    limiter.check("a")


# --- 4: in-memory store -------------------------------------------------------


def test_memory_store_evicts_idle_buckets():
    # Audit 4: buckets were never removed.
    now = [0.0]
    store = InMemoryRateLimitStore(sweep_interval=10, clock=lambda: now[0])
    for index in range(100):
        store.hit(f"key-{index}", 60)
    assert len(store) == 100

    now[0] = 30  # windows still open: nothing to evict
    store.hit("fresh", 60)
    assert len(store) == 101

    now[0] = 100  # every earlier window has closed
    store.hit("later", 60)
    assert len(store) == 1


def test_memory_store_has_a_hard_cap_on_bucket_count():
    now = [0.0]
    store = InMemoryRateLimitStore(max_buckets=50, sweep_interval=10_000, clock=lambda: now[0])
    for index in range(500):
        store.hit(f"attacker-{index}", 60)
    assert len(store) == 50
    # The most recently used buckets are the ones kept.
    assert store.hit("attacker-499", 60).count == 2
    assert store.hit("attacker-0", 60).count == 1


def test_memory_store_counts_within_a_window_and_resets_after():
    now = [0.0]
    store = InMemoryRateLimitStore(clock=lambda: now[0])
    assert [store.hit("k", 10).count for _ in range(3)] == [1, 2, 3]
    assert store.hit("k", 10).reset_after == 10
    now[0] = 10.5
    assert store.hit("k", 10).count == 1


# --- 4: database store --------------------------------------------------------


def migrated_database(tmp_path, name="limits.db") -> Database:
    database = Database(sqlite_path=tmp_path / name)
    database.migrate()
    return database


def test_database_store_counts_per_window(tmp_path):
    now = [1_000_000.0]
    store = DatabaseRateLimitStore(migrated_database(tmp_path), clock=lambda: now[0])
    assert [store.hit("ip:1.2.3.4:/x", 60).count for _ in range(3)] == [1, 2, 3]
    assert store.hit("ip:9.9.9.9:/x", 60).count == 1  # near miss: separate key

    hit = store.hit("ip:1.2.3.4:/x", 60)
    assert 1 <= hit.reset_after <= 60
    now[0] += 60
    assert store.hit("ip:1.2.3.4:/x", 60).count == 1


def test_database_store_is_shared_across_store_instances(tmp_path):
    database = migrated_database(tmp_path)
    now = [2_000_000.0]
    first = DatabaseRateLimitStore(database, clock=lambda: now[0])
    second = DatabaseRateLimitStore(Database(sqlite_path=database.sqlite_path), clock=lambda: now[0])
    limiter_a = RateLimiter(2, 60, store=first, name="shared")
    limiter_b = RateLimiter(2, 60, store=second, name="shared")
    limiter_a.check("client")
    limiter_b.check("client")
    with pytest.raises(HTTPException):
        limiter_a.check("client")


def test_database_store_increment_is_atomic_across_threads(tmp_path):
    database = migrated_database(tmp_path)
    now = 3_000_000.0
    threads_n, hits_each = 8, 25
    barrier = threading.Barrier(threads_n)
    errors = []

    def hammer():
        store = DatabaseRateLimitStore(Database(sqlite_path=database.sqlite_path), clock=lambda: now)
        try:
            barrier.wait(timeout=10)
            for _ in range(hits_each):
                store.hit("contended", 3600)
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=hammer) for _ in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    final = DatabaseRateLimitStore(database, clock=lambda: now).hit("contended", 3600)
    assert final.count == threads_n * hits_each + 1  # no lost updates


def test_database_store_cleans_up_old_windows(tmp_path):
    database = migrated_database(tmp_path)
    now = [4_000_000.0]
    store = DatabaseRateLimitStore(
        database, cleanup_interval=30, retention_seconds=120, clock=lambda: now[0]
    )
    store.hit("old", 60)
    now[0] += 100
    store.hit("recent", 60)
    now[0] += 60  # "old" is past retention, "recent" is not; interval has elapsed
    store.hit("trigger", 60)

    with database.connection() as conn:
        keys = {row["key"] for row in conn.execute("SELECT key FROM rate_limit_buckets")}
    assert keys == {"recent", "trigger"}


def test_fallback_store_keeps_limiting_when_the_primary_fails():
    class Broken:
        def hit(self, key, window_seconds):
            raise ConnectionError("database is down")

        def cleanup(self):
            raise ConnectionError("database is down")

    seen = []
    store = FallbackRateLimitStore(Broken(), InMemoryRateLimitStore(), on_error=seen.append)
    limiter = RateLimiter(2, 60, store=store)
    limiter.check("k")
    limiter.check("k")
    with pytest.raises(HTTPException):
        limiter.check("k")
    assert len(seen) == 3 and isinstance(seen[0], ConnectionError)
    assert store.cleanup() == 0


def test_long_rate_limit_keys_are_hashed_not_truncated():
    store = InMemoryRateLimitStore()
    limiter = RateLimiter(1, 60, store=store, name="long")
    limiter.check("a" * 500 + "1")
    limiter.check("a" * 500 + "2")  # differs only past any truncation point
    assert len(store) == 2


# --- 3: client address --------------------------------------------------------


class FakeRequest:
    def __init__(self, peer, forwarded=None):
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
        self.client = type("Client", (), {"host": peer})() if peer else None


def test_forwarded_for_is_ignored_unless_proxies_are_trusted():
    # Audit 3: the header was trusted unconditionally.
    request = FakeRequest("10.0.0.1", "6.6.6.6")
    assert client_ip(request, 0) == "10.0.0.1"
    assert client_ip(request) == "10.0.0.1"  # default is zero trusted proxies


def test_nth_address_from_the_right_is_used():
    chain = "6.6.6.6, 203.0.113.5, 10.1.1.1"
    assert client_ip(FakeRequest("10.0.0.1", chain), 1) == "10.1.1.1"
    assert client_ip(FakeRequest("10.0.0.1", chain), 2) == "203.0.113.5"
    # A client cannot push its own entry into the trusted position.
    assert client_ip(FakeRequest("10.0.0.1", "1.1.1.1, 2.2.2.2, 203.0.113.5"), 1) == "203.0.113.5"


@pytest.mark.parametrize(
    "forwarded",
    ["not-an-ip", "<script>", "999.1.1.1", "", "203.0.113.5; DROP TABLE", "unknown"],
)
def test_unparseable_forwarded_values_fall_back_to_the_peer(forwarded):
    assert client_ip(FakeRequest("10.0.0.1", forwarded), 1) == "10.0.0.1"


def test_short_chain_and_missing_peer_are_handled():
    assert client_ip(FakeRequest("10.0.0.1", "203.0.113.5"), 3) == "10.0.0.1"
    assert client_ip(FakeRequest(None), 1) == "unknown"


def test_ipv6_and_ports_are_normalised():
    assert client_ip(FakeRequest("10.0.0.1", "[2001:db8::1]:443"), 1) == "2001:db8::1"
    assert client_ip(FakeRequest("10.0.0.1", "203.0.113.5:51234"), 1) == "203.0.113.5"
    assert coarse_ip("203.0.113.77") == "203.0.113.0/24"
    assert coarse_ip("2001:db8:abcd:12::1") == "2001:db8:abcd::/48"
    assert coarse_ip("garbage") == "unknown"


# --- 13: origin allow-list ----------------------------------------------------

ALLOWED = frozenset({"https://app.example", "http://127.0.0.1:8000"})


@pytest.mark.parametrize(
    "headers,has_cookies,expected",
    [
        ({"origin": "https://app.example"}, True, None),
        ({"origin": "HTTPS://APP.EXAMPLE:443"}, True, None),
        ({"referer": "https://app.example/#/reset-password"}, True, None),
        ({}, False, None),
        ({}, True, "origin_required"),
        ({"origin": "https://evil.example"}, False, "origin_not_allowed"),
        ({"origin": "null"}, True, "origin_not_allowed"),
        ({"origin": "https://app.example.evil.example"}, True, "origin_not_allowed"),
        ({"origin": "http://app.example"}, True, "origin_not_allowed"),
        ({"origin": "https://evil.example", "referer": "https://app.example/"}, True, "origin_not_allowed"),
        ({"referer": "https://evil.example/https://app.example"}, False, "origin_not_allowed"),
    ],
)
def test_origin_allow_list(headers, has_cookies, expected):
    assert origin_problem(headers, ALLOWED, has_cookies=has_cookies) == expected


# --- 7: log redaction ---------------------------------------------------------


def test_redact_masks_sensitive_keys_at_any_depth():
    payload = {
        "event": "x",
        "reset_link": "https://a/#/reset-password?token=abc",
        "nested": {"Authorization": "Bearer abc.def", "list": [{"api_key": "re_123456789"}]},
        "user": {"password": "hunter2", "email": "a@example.com"},
        "cookie": "arranger_session=abc",
        "csrf_token": "t",
        "client_secret": "s",
        "has_resend_key": True,
        "password_reset_from": "Arranger <no-reply@example.com>",
        "status_code": 403,
    }
    clean = redact(payload)
    assert clean["reset_link"] == REDACTED
    assert clean["nested"]["Authorization"] == REDACTED
    assert clean["nested"]["list"][0]["api_key"] == REDACTED
    assert clean["user"] == {"password": REDACTED, "email": "a@example.com"}
    assert clean["cookie"] == clean["csrf_token"] == clean["client_secret"] == REDACTED
    # Near misses: non-secret fields survive, including ones that merely sound secret.
    assert clean["has_resend_key"] is True
    assert clean["password_reset_from"] == "Arranger <no-reply@example.com>"
    assert clean["status_code"] == 403 and clean["event"] == "x"
    assert payload["user"]["password"] == "hunter2"  # input is not mutated


def test_scrub_text_masks_secrets_inside_strings():
    assert "abc123" not in scrub_text("GET /#/reset-password?token=abc123&x=1")
    assert "x=1" in scrub_text("GET /?token=abc123&x=1")
    assert scrub_text("Authorization: Bearer eyJhbGciOi.payload") == "Authorization: Bearer [REDACTED]"
    assert "re_AbCdEf123456" not in scrub_text("key re_AbCdEf123456 rejected")
    assert scrub_text("error code: 1010") == "error code: 1010"  # near miss


def capture(logger_name="arranger_api"):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    return records, lambda: logger.removeHandler(handler)


def test_structured_logs_are_redacted_for_every_handler():
    configure_logging("INFO")
    records, detach = capture()
    child = get_logger("arranger_api.email")
    try:
        log_event(child, "probe", reset_link="https://x/#/reset-password?token=SECRETTOKEN", ok=1)
        child.info("sending", extra={"reset_link": "https://x/?token=SECRETTOKEN", "attempt": 2})
        child.info("context %s", {"password": "hunter2", "user": "a"})
        child.info("fetch https://x/?reset_token=SECRETTOKEN failed")
    finally:
        detach()

    assert len(records) == 4
    formatter = JsonLogFormatter()
    for record in records:
        rendered = formatter.format(record)
        assert "SECRETTOKEN" not in rendered and "hunter2" not in rendered
        assert "SECRETTOKEN" not in record.getMessage()
    assert json.loads(records[0].getMessage()) == {"event": "probe", "ok": 1, "reset_link": REDACTED}
    assert records[1].reset_link == REDACTED and records[1].attempt == 2


# --- 16: logging configuration ------------------------------------------------


def test_configure_logging_sets_the_package_logger_explicitly_and_idempotently():
    # Audit 16: logging.basicConfig is a no-op when the root logger already has
    # handlers (as under pytest), which left this logger at WARNING.
    root = logging.getLogger()
    assert root.handlers, "pytest installs root handlers; that is the situation under test"
    logger = logging.getLogger("arranger_api")
    root_before = (root.level, list(root.handlers))

    configure_logging("DEBUG")
    configure_logging("INFO")
    configure_logging("INFO")

    assert logger.level == logging.INFO
    assert logger.isEnabledFor(logging.INFO)
    own = [h for h in logger.handlers if getattr(h, "_arranger_api_handler", False)]
    assert len(own) == 1
    assert isinstance(own[0].formatter, JsonLogFormatter)
    assert any(isinstance(f, RedactionFilter) for f in own[0].filters)
    assert logger.propagate is True
    assert (root.level, list(root.handlers)) == root_before  # the root logger is left alone


def test_info_events_reach_caplog_under_pytest(caplog):
    configure_logging("INFO")
    with caplog.at_level(logging.INFO, logger="arranger_api"):
        log_event(logging.getLogger("arranger_api"), "caplog_probe", value=1)
    assert any('"event": "caplog_probe"' in r.getMessage() for r in caplog.records)


def test_json_formatter_merges_events_and_includes_request_id():
    from arranger_api.observability import request_id_var

    configure_logging("INFO")
    records, detach = capture()
    token = request_id_var.set("req-abc-12345")
    try:
        log_event(logging.getLogger("arranger_api"), "formatted", n=1)
        try:
            raise RuntimeError("boom password=hunter2")
        except RuntimeError:
            get_logger("arranger_api").exception("failed")
    finally:
        request_id_var.reset(token)
        detach()

    formatter = JsonLogFormatter()
    event = json.loads(formatter.format(records[0]))
    assert event["event"] == "formatted" and event["n"] == 1
    assert event["request_id"] == "req-abc-12345"
    assert {"ts", "level", "logger"} <= set(event)
    failure = json.loads(formatter.format(records[1]))
    assert failure["message"] == "failed" and failure["request_id"] == "req-abc-12345"
    assert "RuntimeError" in failure["exception"] and "hunter2" not in failure["exception"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
