"""Request hardening through the HTTP surface.

Each test names the audit finding it pins. Every rule-like control has a
near-miss beside it showing legitimate traffic still gets through.
"""

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, Request  # noqa: E402

from arranger.limits import MAX_BAR_NUMBER, MAX_PLAN_SECTIONS  # noqa: E402
from arranger_api.main import create_app  # noqa: E402
from conftest import (  # noqa: E402
    ORIGIN,
    PASSWORD,
    PLAN,
    PROFILE,
    SCORE,
    build_settings,
    csrf_headers,
    make_client,
    memory_storage,
    production_settings,
    register,
)


def app_with_probe_routes(settings):
    """A fresh app plus routes that only exist to exercise the middleware.

    Built without the static frontend: that mount is a catch-all at "/", and
    routes added after it would never be reached.
    """
    app = create_app(replace(settings, frontend_dir=Path("__no_frontend_here__")))
    probe = APIRouter()

    @probe.post("/imports/echo-size")
    async def upload_size(request: Request) -> dict:
        return {"bytes": len(await request.body())}

    @probe.post("/echo-size")
    async def plain_size(request: Request) -> dict:
        return {"bytes": len(await request.body())}

    @probe.get("/boom")
    def boom() -> dict:
        raise RuntimeError("SELECT * FROM users WHERE x failed at C:\\srv\\secret\\db.py")

    @probe.get("/whoami-ip")
    def whoami_ip(request: Request) -> dict:
        from arranger_api.security import client_ip

        return {"ip": client_ip(request)}

    app.include_router(probe)
    return app


# --- 1. body size limit -------------------------------------------------------


def test_chunked_body_over_the_limit_is_rejected_while_streaming():
    # Audit 1: only Content-Length was checked, so a chunked body walked past it.
    api = make_client(settings=build_settings(max_request_bytes=10_000))

    def chunks():
        yield b'{"score": {"title": "'
        for _ in range(40):
            yield b"a" * 1_000
        yield b'"}}'

    response = api.post("/verify", content=chunks(), headers={"content-type": "application/json"})
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "request_too_large"


def test_chunked_body_under_the_limit_is_processed():
    api = make_client(settings=build_settings(max_request_bytes=10_000))
    body = json.dumps({"score": SCORE, "profile": PROFILE}).encode()

    def chunks():
        yield body[:50]
        yield body[50:]

    response = api.post("/verify", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 200


def test_lying_content_length_cannot_smuggle_a_larger_body():
    app = app_with_probe_routes(build_settings(max_request_bytes=1_000))
    api = make_client(app=app)
    response = api.post(
        "/echo-size",
        content=b"x" * 5_000,
        headers={"content-length": "10"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize("value", ["abc", "-5", "1e3", "12 34", ""])
def test_malformed_content_length_is_a_400(value):
    api = make_client()
    response = api.post(
        "/verify",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": value},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_content_length"


def test_upload_prefixes_get_the_larger_cap_and_other_paths_do_not():
    settings = build_settings(max_request_bytes=1_000, max_upload_bytes=50_000)
    api = make_client(app=app_with_probe_routes(settings))
    payload = b"x" * 20_000

    assert api.post("/imports/echo-size", content=payload).json() == {"bytes": 20_000}
    assert api.post("/echo-size", content=payload).status_code == 413
    # The large cap is still a cap.
    assert api.post("/imports/echo-size", content=b"x" * 60_000).status_code == 413


def test_upload_prefix_must_match_a_whole_path_segment():
    from arranger_api.middleware import body_limit_for

    settings = build_settings(max_request_bytes=1_000, max_upload_bytes=50_000)
    assert body_limit_for("/imports", settings) == 50_000
    assert body_limit_for("/projects/abc/audio", settings) == 50_000
    assert body_limit_for("/importsx", settings) == 1_000
    assert body_limit_for("/auth/login", settings) == 1_000


def test_upload_limit_defaults():
    settings = build_settings()
    assert settings.max_request_bytes == 1_000_000
    assert settings.max_upload_bytes == 40 * 1024 * 1024
    assert settings.upload_path_prefixes == ("/imports", "/projects")


# --- 2. schema bounds ---------------------------------------------------------


def _score_with(note_overrides=None, **score_overrides):
    note = dict(SCORE["notes"][0], **(note_overrides or {}))
    return dict(SCORE, notes=[note], **score_overrides)


def test_one_note_with_a_billion_bars_is_rejected():
    # Audit 2: `bar` had no upper bound.
    api = make_client()
    response = api.post(
        "/verify", json={"score": _score_with({"bar": 1_000_000_000}), "profile": PROFILE}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "note",
    [
        {"pitch": 128},
        {"pitch": -1},
        {"onset": -0.1},
        {"onset": 6 * 3600 + 1},
        {"duration": 0},
        {"duration": 601},
        {"bar": MAX_BAR_NUMBER + 1},
        {"bar": 0},
    ],
)
def test_out_of_range_note_values_are_rejected(note):
    response = make_client().post(
        "/verify", json={"score": _score_with(note), "profile": PROFILE}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("tempo", [4.9, 1000.5, 0])
def test_out_of_range_tempo_is_rejected(tempo):
    response = make_client().post(
        "/verify", json={"score": _score_with(tempo_bpm=tempo), "profile": PROFILE}
    )
    assert response.status_code == 422


def test_values_on_the_boundary_are_accepted():
    # Near miss: the largest legal values must still work.
    note = {"pitch": 127, "onset": 0.0, "duration": 600, "bar": MAX_BAR_NUMBER}
    score = dict(SCORE, tempo_bpm=1000, notes=[note])
    response = make_client().post("/verify", json={"score": score, "profile": PROFILE})
    assert response.status_code == 200


def test_plan_lists_and_bars_are_bounded():
    api = make_client()
    section = PLAN["sections"][0]

    too_many = dict(PLAN, sections=[section] * (MAX_PLAN_SECTIONS + 1))
    huge_bar = dict(PLAN, sections=[dict(section, end_bar=1_000_000_000)])
    huge_pedal = dict(PLAN, pedal_bars=[1_000_000_000])
    long_text = dict(PLAN, notes="x" * 5_000)
    for plan in (too_many, huge_bar, huge_pedal, long_text):
        response = api.post("/render", json={"source": SCORE, "plan": plan})
        assert response.status_code == 422, plan.keys()

    assert api.post("/render", json={"source": SCORE, "plan": PLAN}).status_code == 200


def test_max_attempts_is_bounded():
    api = make_client()
    body = {"source": SCORE, "profile": PROFILE, "max_attempts": 1_000}
    assert api.post("/arrange/dry-run", json=body).status_code == 422
    body["max_attempts"] = 2
    assert api.post("/arrange/dry-run", json=body).status_code == 200


def test_section_schema_mirrors_the_plan_dataclass():
    # Stored plans are `asdict(plan)`; a field the API schema does not know
    # would make every saved plan unrenderable.
    import typing

    from arranger.plan import LHPattern, Section
    from arranger_api.schemas import SectionIn

    assert set(SectionIn.model_fields) == set(Section.__dataclass_fields__)
    patterns = set(typing.get_args(SectionIn.model_fields["lh_pattern"].annotation))
    assert patterns == {pattern.value for pattern in LHPattern}


def test_profile_schema_mirrors_the_profile_dataclass():
    from arranger.profile import PlayerProfile
    from arranger_api.schemas import PlayerProfileIn, to_profile

    assert set(PlayerProfileIn.model_fields) == set(PlayerProfile.__dataclass_fields__)
    # Schema defaults are the domain defaults, and they round-trip.
    assert to_profile(PlayerProfileIn()) == PlayerProfile()


def test_saved_records_are_read_back_with_the_domain_parsers():
    # A saved profile/plan/score carries every field the domain model has,
    # including ones added after the request schema was written.
    api = make_client(memory_storage())
    register(api)
    headers = csrf_headers(api)
    profile_body = dict(PROFILE, left_max_span=10, right_fingers=[1, 2, 3, 5], max_repeat_rate=6.5)
    profile = api.post("/profiles", json=profile_body, headers=headers).json()["record"]
    assert profile["payload"]["left_max_span"] == 10
    assert profile["payload"]["right_fingers"] == [1, 2, 3, 5]
    score = api.post("/scores", json=SCORE, headers=headers).json()["record"]
    section = dict(PLAN["sections"][0], voicing="smooth", bass="source", harmonic_rhythm="detected")
    plan = api.post(
        "/plans",
        json={"score_id": score["id"], "plan": dict(PLAN, sections=[section])},
        headers=headers,
    ).json()["record"]
    assert plan["payload"]["sections"][0]["voicing"] == "smooth"

    rendered = api.post(
        "/arrangements/render-and-verify",
        json={"score_id": score["id"], "profile_id": profile["id"], "plan_id": plan["id"]},
        headers=headers,
    )
    assert rendered.status_code == 200, rendered.text

    bad_fingers = api.post("/profiles", json=dict(PROFILE, left_fingers=[1, 1, 9]), headers=headers)
    assert bad_fingers.status_code == 422


def test_validation_errors_do_not_echo_the_rejected_input():
    api = make_client(memory_storage())
    response = api.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "hunter2-hunter2-secret", "display_name": ""},
    )
    assert response.status_code == 422
    assert "hunter2" not in response.text
    assert all(set(item) == {"type", "loc", "msg"} for item in response.json()["detail"])


# --- 3. X-Forwarded-For -------------------------------------------------------


def test_forwarded_for_is_ignored_by_default():
    # Audit 3: the first X-Forwarded-For entry was trusted unconditionally.
    api = make_client(app=app_with_probe_routes(build_settings()))
    response = api.get("/whoami-ip", headers={"X-Forwarded-For": "6.6.6.6"})
    assert response.json()["ip"] != "6.6.6.6"


def test_forwarded_for_is_used_when_a_proxy_is_trusted():
    api = make_client(app=app_with_probe_routes(build_settings(trusted_proxy_count=1)))
    response = api.get("/whoami-ip", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert response.json()["ip"] == "203.0.113.9"


# --- 5. error bodies ----------------------------------------------------------


def test_unexpected_exceptions_return_only_code_message_and_request_id(caplog):
    # Audit 5: str(exc) went to the client with the 500.
    api = make_client(app=app_with_probe_routes(build_settings()), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="arranger_api"):
        response = api.get("/boom")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert set(detail) == {"error", "detail", "request_id"}
    assert detail["detail"] == "Internal server error."
    assert detail["request_id"] == response.headers["x-request-id"]
    for leaked in ("SELECT", "secret", "db.py", "RuntimeError"):
        assert leaked not in response.text
    # The real exception is in the log, under the same request id.
    logged = [r for r in caplog.records if r.exc_info and "SELECT" in str(r.exc_info[1])]
    assert logged and logged[0].request_id == detail["request_id"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_domain_error_hides_internal_text_but_keeps_validation_messages():
    from arranger.render import RenderError
    from arranger_api.errors import domain_error

    leaky = domain_error(RuntimeError("SELECT * FROM users failed at C:\\secret\\path.py"))
    assert leaky.status_code == 500
    assert leaky.detail == {"error": "internal_error", "detail": "Internal server error."}

    # A ValueError that happens to carry SQL or a path is still not shown.
    for text in (
        'near "SELECT": syntax error',
        "could not open /var/lib/arranger/data.db",
        "bad value in C:\\Users\\app\\config.py",
        "UNIQUE constraint failed: users.email",
    ):
        error = domain_error(ValueError(text))
        assert error.status_code == 400
        assert error.detail["detail"] == "The request could not be processed as sent."

    # Near miss: ordinary domain messages pass through untouched.
    plain = domain_error(ValueError("section 0: end_bar 1 is before start_bar 2"))
    assert plain.detail == {
        "error": "invalid_input",
        "detail": "section 0: end_bar 1 is before start_bar 2",
    }
    render = domain_error(RenderError("no melody found in bars 3-4"))
    assert render.status_code == 400
    assert render.detail == {"error": "invalid_plan", "detail": "no melody found in bars 3-4"}


def test_import_errors_show_their_public_message_not_their_detail():
    from arranger.limits import LimitExceeded, MalformedFile
    from arranger_api.errors import domain_error

    malformed = domain_error(MalformedFile("The score data is not valid.", detail="offset 0x3f"))
    assert malformed.status_code == 400
    assert malformed.detail == {"error": "malformed_file", "detail": "The score data is not valid."}
    assert domain_error(LimitExceeded("Too many notes.")).status_code == 413


def test_http_5xx_raised_by_a_route_is_sanitised_too():
    from arranger_api.errors import public_error_body

    body = public_error_body(503, {"error": "x", "detail": "psycopg.OperationalError: host db"}, "rid-12345")
    assert body == {
        "detail": {
            "error": "internal_error",
            "detail": "Service temporarily unavailable.",
            "request_id": "rid-12345",
        }
    }
    assert public_error_body(404, {"error": "not_found", "detail": "gone"}) == {
        "detail": {"error": "not_found", "detail": "gone"}
    }


# --- 6. CORS ------------------------------------------------------------------


def _preflight(api, origin, method="POST", request_headers="content-type"):
    return api.options(
        "/scores",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": request_headers,
        },
    )


def test_null_origin_is_not_allowed_with_credentials():
    # Audit 6: "null" was a default origin alongside allow_credentials=True.
    assert "null" not in build_settings().cors_origins
    response = _preflight(make_client(), "null")
    assert response.headers.get("access-control-allow-origin") is None


def test_configured_origin_passes_preflight_with_restricted_methods_and_headers():
    api = make_client()
    allowed = _preflight(api, ORIGIN, request_headers="content-type,x-csrf-token")
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"
    methods = {m.strip() for m in allowed.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PUT", "DELETE", "OPTIONS"}

    assert _preflight(api, ORIGIN, method="TRACE").status_code == 400
    assert _preflight(api, ORIGIN, request_headers="x-evil").status_code == 400
    assert _preflight(api, "https://evil.example").headers.get("access-control-allow-origin") is None


# --- 10. cache headers --------------------------------------------------------


def test_every_api_response_is_no_store():
    # Audit 10: only /auth, /diagnostics, /ready and /version were marked.
    api = make_client(memory_storage())
    register(api)
    headers = csrf_headers(api)
    responses = [
        api.get("/health"),
        api.get("/scores"),
        api.post("/scores", json=SCORE, headers=headers),
        api.post("/verify", json={"score": SCORE, "profile": PROFILE}),
        api.get("/scores/does-not-exist"),
        api.post("/verify", json={}),
        api.get("/auth/me"),
    ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.request.url
        assert "Cookie" in response.headers["vary"]


def test_static_frontend_assets_stay_cacheable():
    api = make_client()
    page = api.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"  # revalidate HTML so deploys show up
    # The app's own modules are not fingerprinted, so they are revalidated too: a
    # deploy must never leave a browser running a new module against an old one.
    module = api.get("/js/app.js")
    assert module.status_code == 200
    assert module.headers["cache-control"] == "no-cache"
    assert module.headers.get("etag") or module.headers.get("last-modified")
    assert api.get("/styles.css").headers["cache-control"] == "no-cache"
    # A miss under the static mount is an error response, not an asset.
    assert api.get("/missing.js").headers["cache-control"] == "no-store"


# --- 19. security headers -----------------------------------------------------


def test_security_headers_are_complete():
    response = make_client().get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert "camera=()" in response.headers["permissions-policy"]

    csp = {
        part.split()[0]: part.split()[1:]
        for part in response.headers["content-security-policy"].split("; ")
    }
    assert csp["frame-ancestors"] == ["'none'"]
    assert "'wasm-unsafe-eval'" in csp["script-src"]
    assert "'unsafe-inline'" not in csp["script-src"]
    assert "'unsafe-eval'" not in csp["script-src"]
    assert "blob:" in csp["worker-src"]
    assert "blob:" in csp["media-src"]
    assert csp["object-src"] == ["'none'"]


def test_security_headers_cover_static_files_and_middleware_rejections():
    api = make_client()
    rejected = api.post(
        "/verify", content=b"{}", headers={"content-type": "application/json", "content-length": "x"}
    )
    for response in (api.get("/"), rejected):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-request-id"]


def test_hsts_is_production_only():
    assert "strict-transport-security" not in make_client().get("/health").headers
    production = make_client(settings=production_settings()).get("/health")
    assert production.headers["strict-transport-security"].startswith("max-age=31536000")


def test_interactive_docs_are_disabled_in_production():
    assert make_client(settings=production_settings()).get("/openapi.json").status_code == 404
    assert make_client().get("/openapi.json").status_code == 200


# --- 20. request ids and metrics ----------------------------------------------


def test_request_id_is_generated_and_safe_inbound_ids_are_kept():
    api = make_client()
    generated = api.get("/health").headers["x-request-id"]
    assert len(generated) >= 8
    kept = api.get("/health", headers={"X-Request-ID": "trace_ABC-12345"})
    assert kept.headers["x-request-id"] == "trace_ABC-12345"


@pytest.mark.parametrize("bad", ["short", "has space 123", "x" * 65, "<script>alert(1)</script>", "a\tb12345678"])
def test_unsafe_inbound_request_ids_are_replaced(bad):
    response = make_client().get("/health", headers={"X-Request-ID": bad})
    assert response.headers["x-request-id"] != bad
    assert len(response.headers["x-request-id"]) == 36


def test_request_id_is_on_every_log_line(caplog):
    api = make_client()
    with caplog.at_level(logging.INFO, logger="arranger_api"):
        response = api.get("/health", headers={"X-Request-ID": "log-line-trace-1"})
    assert response.status_code == 200
    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    access = [e for e in events if e.get("event") == "http_request"]
    assert access and access[-1]["request_id"] == "log-line-trace-1"
    assert access[-1]["route"] == "/health"


def test_metrics_endpoint_is_hidden_without_a_token():
    api = make_client()
    assert api.get("/metrics").status_code == 404
    assert api.get("/metrics", headers={"Authorization": "Bearer anything"}).status_code == 404


def test_metrics_endpoint_requires_the_bearer_token():
    api = make_client(settings=build_settings(metrics_token="metrics-secret-token"))
    assert api.get("/metrics").status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Basic metrics-secret-token"}).status_code == 401
    ok = api.get("/metrics", headers={"Authorization": "Bearer metrics-secret-token"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain; version=0.0.4")


def test_metrics_count_requests_by_route_template_and_status_class():
    settings = build_settings(metrics_token="metrics-secret-token")
    api = make_client(memory_storage(), settings=settings)
    register(api)
    headers = csrf_headers(api)
    score_id = api.post("/scores", json=SCORE, headers=headers).json()["record"]["id"]
    api.get(f"/scores/{score_id}")
    api.get("/scores/00000000-0000-0000-0000-000000000000")
    api.post("/auth/login", json={"email": "user@example.com", "password": "wrong-password-1"})

    text = api.get("/metrics", headers={"Authorization": "Bearer metrics-secret-token"}).text
    assert "# TYPE arranger_http_requests_total counter" in text
    assert 'arranger_http_requests_total{method="GET",route="/scores/{score_id}",status_class="2xx"} 1' in text
    assert 'arranger_http_requests_total{method="GET",route="/scores/{score_id}",status_class="4xx"} 1' in text
    assert score_id not in text  # raw ids never become label values
    assert 'arranger_auth_failures_total{reason="bad_credentials"} 1' in text
    assert "# TYPE arranger_http_request_duration_seconds histogram" in text
    assert 'arranger_http_request_duration_seconds_bucket{route="/scores/{score_id}",le="+Inf"} 2' in text


def test_health_touches_no_dependencies():
    # /health must answer even when the database cannot be reached at all.
    settings = build_settings(
        sqlite_path="Z:/definitely/not/a/real/dir/x.db", rate_limit_backend="database"
    )
    api = make_client(settings=settings)
    assert api.get("/health").status_code == 200


# --- 21. /diagnostics/email ---------------------------------------------------


def test_email_diagnostics_is_not_public():
    # Audit 21: it was unauthenticated.
    assert make_client().get("/diagnostics/email").status_code == 404
    api = make_client(settings=build_settings(metrics_token="metrics-secret-token"))
    assert api.get("/diagnostics/email").status_code == 401
    ok = api.get("/diagnostics/email", headers={"Authorization": "Bearer metrics-secret-token"})
    assert ok.status_code == 200
    assert ok.json()["provider"] == "console"


# --- 4. rate limiting through the app -----------------------------------------


def test_rate_limit_is_keyed_by_route_template_and_returns_retry_after():
    # Audit 4: keyed by raw path, so /scores/<new id> was a fresh bucket each time.
    settings = build_settings(
        read_rate_limit_requests=3, read_rate_limit_window_seconds=60, metrics_token="m-token-12345"
    )
    api = make_client(memory_storage(), settings=settings)
    statuses = [api.get(f"/scores/id-{index}").status_code for index in range(5)]
    assert statuses == [401, 401, 401, 429, 429]

    limited = api.get("/scores/id-99")
    assert limited.json()["detail"]["error"] == "rate_limited"
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    # Near miss: a different route has its own budget.
    assert api.get("/profiles").status_code == 401

    text = api.get("/metrics", headers={"Authorization": "Bearer m-token-12345"}).text
    assert 'arranger_rate_limit_hits_total{limiter="read"}' in text


def test_get_requests_have_a_generous_default_limit():
    settings = build_settings()
    assert settings.read_rate_limit_requests >= 5 * settings.rate_limit_requests
    api = make_client()
    assert all(api.get("/version").status_code == 200 for _ in range(150))


def test_authenticated_requests_are_also_limited_per_user():
    settings = build_settings(read_rate_limit_requests=4, trusted_proxy_count=1)
    api = make_client(memory_storage(), settings=settings)
    register(api, "limited@example.com", PASSWORD)
    assert [api.get("/scores").status_code for _ in range(3)] == [200, 200, 200]

    # The same user arriving from another address has a fresh per-IP bucket but
    # shares the per-user one: request 4 passes, request 5 does not.
    elsewhere = {"X-Forwarded-For": "198.51.100.7"}
    assert api.get("/scores", headers=elsewhere).status_code == 200
    assert api.get("/scores", headers=elsewhere).status_code == 429


def test_password_reset_has_its_own_tighter_limit():
    settings = build_settings(
        password_reset_rate_limit_requests=2, auth_rate_limit_requests=50
    )
    api = make_client(memory_storage(), settings=settings)
    body = {"email": "nobody@example.com"}
    statuses = [api.post("/auth/password-reset/request", json=body).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    assert api.post("/auth/login", json={"email": "a@example.com", "password": "x"}).status_code == 401


def test_upload_prefix_flood_guard_rejects_before_reading_the_body():
    settings = build_settings(upload_rate_limit_requests=2, max_upload_bytes=50_000)
    api = make_client(app=app_with_probe_routes(settings))
    statuses = [api.post("/imports/echo-size", content=b"x" * 10).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
