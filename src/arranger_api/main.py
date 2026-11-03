"""FastAPI entry point for Arranger.

`create_app(settings)` builds a fully wired application; `app` at the bottom of
this module is the default instance uvicorn serves. Nothing here reads
configuration at request time from module globals: routes get what they need
from `request.app.state` through the dependencies in `arranger_api.deps`.
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from arranger.adapters.lilypond import LilyPondEngraver
from arranger.agent import ScriptedModel
from arranger.application import (
    arrange_score,
    fidelity_for,
    plan_analysis,
    plan_score,
    render_plan,
    verify_score,
)
from arranger.plan import ArrangementPlan, LHPattern, Section
from arranger.render import last_bar

from .auth import CurrentUser, PasswordService
from .auth_routes import router as auth_router
from .deps import (
    enforce_rate_limit,
    get_current_user,
    get_email_health,
    get_email_sender,
    get_settings,
    get_storage,
    require_verified_user,
)
from .email import EmailHealth, build_email_sender, email_diagnostics
from .errors import api_error, conflict, domain_error, not_found, public_error_body, service_unavailable
from .artifacts import build_artifact_store
from .jobs import JobRunner, JobServices
from .metrics import AppMetrics
from .middleware import BodyLimitMiddleware, RequestContextMiddleware, RequestGuardMiddleware
from .observability import configure_logging, get_logger, log_event
from .queue import build_job_queue
from .schemas import (
    ArrangeRequest,
    ArrangeResponse,
    ArrangementPlanIn,
    FeedbackCreateRequest,
    PersistentArrangeRequest,
    PersistentRenderVerifyRequest,
    PlanAnalysisResponse,
    PlanCreateRequest,
    PlanResponse,
    PlayerProfileIn,
    RecordResponse,
    RecordsResponse,
    RenderRequest,
    RenderResponse,
    RenderVerifyRequest,
    RenderVerifyResponse,
    ScoreIn,
    VerifyRequest,
    fidelity_to_dict,
    plan_from_payload,
    profile_from_payload,
    run_result_to_dict,
    score_from_payload,
    score_to_dict,
    stored_profile_dict,
    stored_score_dict,
    to_plan,
    to_profile,
    to_score,
    verdict_to_dict,
)
from .security import FallbackRateLimitStore, InMemoryRateLimitStore, RateLimits, RateLimitStore
from .settings import Settings, load_settings, validate_settings
from .storage import Database, DatabaseUnavailable, Storage
from .storage.rate_limits import DatabaseRateLimitStore

logger = get_logger("arranger_api")

__all__ = [
    "app",
    "create_app",
    "get_current_user",
    "get_email_sender",
    "get_settings",
    "get_storage",
    "require_verified_user",
]

CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = ["Accept", "Content-Type", "X-CSRF-Token", "X-Request-ID"]
CORS_EXPOSE_HEADERS = ["X-Request-ID", "Retry-After"]

system_router = APIRouter(tags=["system"])
router = APIRouter()


# --- system endpoints --------------------------------------------------------


def require_metrics_token(request: Request) -> None:
    """Bearer-token gate for operational endpoints. 404 when no token is configured,
    so an unconfigured deployment does not even admit the endpoint exists."""
    expected = request.app.state.settings.metrics_token
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented.strip().encode(), expected.encode()
    ):
        request.app.state.metrics.auth_failures.inc(reason="metrics_token")
        raise api_error(
            401,
            "unauthorized",
            "A valid bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@system_router.get("/health")
def health() -> dict:
    """Liveness. Touches nothing: no database, no email, no rate-limit store."""
    return {"status": "ok", "service": "arranger-api"}


@system_router.get("/version")
def version(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "service": "arranger-api",
        "version": __version__,
        "commit_sha": settings.commit_sha,
        "environment": settings.app_env,
    }


ARTIFACT_HEALTH_TTL_SECONDS = 30.0


def _artifact_store_is_healthy(app: FastAPI) -> bool:
    """Ask the file store whether it works, at most once every 30 seconds.

    For an object store the check is a network call, and readiness probes arrive
    every few seconds from every load balancer.
    """
    store = getattr(app.state, "artifacts", None)
    if store is None:
        return True
    checked_at, healthy = getattr(app.state, "artifact_health", (0.0, False))
    now = time.monotonic()
    if now - checked_at > ARTIFACT_HEALTH_TTL_SECONDS or not healthy:
        try:
            healthy = bool(store.healthy())
        except Exception as exc:  # a store that raises is not healthy
            logger.error("artifact_store_check_failed", exc_info=exc)
            healthy = False
        app.state.artifact_health = (now, healthy)
    return healthy


@system_router.get("/ready")
def ready(
    request: Request,
    storage: Storage = Depends(get_storage),
    email_health: EmailHealth = Depends(get_email_health),
) -> dict:
    """Readiness: database reachable, schema current, files storable, and how email is doing."""
    try:
        migrations = storage.migration_status()
        if migrations["pending"]:
            raise service_unavailable("migrations_pending")
        storage.ping()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("readiness_check_failed", exc_info=exc)
        raise service_unavailable("database_unavailable") from exc
    # Uploads, arrangements and downloads all need the file store. A server that
    # cannot reach it should be taken out of rotation, not fail request by request.
    if not _artifact_store_is_healthy(request.app):
        raise service_unavailable("artifact_store_unavailable")
    queue = getattr(request.app.state, "job_queue", None)
    if queue is not None:
        try:
            queue_ok = bool(queue.healthy())
        except Exception as exc:  # an unreachable queue is not ready, whatever the error
            logger.error("job_queue_check_failed", exc_info=exc)
            queue_ok = False
        if not queue_ok:
            raise service_unavailable("job_queue_unavailable")
    return {
        "status": "ready",
        "service": "arranger-api",
        "database": "ok",
        "artifacts": "ok",
        "queue": queue.name if queue is not None else "database",
        "migrations": {"current": migrations["current"], "pending": 0},
        "email": email_health.status(),
    }


@system_router.get("/metrics", include_in_schema=False, dependencies=[Depends(require_metrics_token)])
def metrics_endpoint(request: Request) -> PlainTextResponse:
    metrics: AppMetrics = request.app.state.metrics
    return PlainTextResponse(metrics.render(), media_type=metrics.registry.content_type)


@system_router.get(
    "/diagnostics/email",
    include_in_schema=False,
    dependencies=[Depends(require_metrics_token)],
)
def email_diagnostics_endpoint(settings: Settings = Depends(get_settings)) -> dict:
    return email_diagnostics(settings)


# --- stateless compute -------------------------------------------------------


@router.post("/verify")
def verify_endpoint(request: VerifyRequest) -> dict:
    try:
        score = to_score(request.score)
        profile = to_profile(request.profile)
        return verdict_to_dict(verify_score(score, profile))
    except Exception as exc:
        raise domain_error(exc) from exc


@router.post("/render", response_model=RenderResponse)
def render_endpoint(request: RenderRequest) -> dict:
    try:
        source = to_score(request.source)
        plan = to_plan(request.plan)
        arranged = render_plan(plan, source)
        return {"score": score_to_dict(arranged)}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.post("/render-and-verify", response_model=RenderVerifyResponse)
def render_and_verify_endpoint(request: RenderVerifyRequest) -> dict:
    try:
        source = to_score(request.source)
        profile = to_profile(request.profile)
        plan = to_plan(request.plan)
        arranged = render_plan(plan, source)
        verdict = verify_score(arranged, profile)
        fidelity = fidelity_for(source, arranged)
        return {
            "arranged": score_to_dict(arranged),
            "verdict": verdict_to_dict(verdict),
            "fidelity": fidelity_to_dict(fidelity),
        }
    except Exception as exc:
        raise domain_error(exc) from exc


@router.post("/arrange/dry-run", response_model=ArrangeResponse)
def arrange_dry_run_endpoint(request: ArrangeRequest) -> dict:
    try:
        source = to_score(request.source)
        profile = to_profile(request.profile)
        end = last_bar(source)
        model = ScriptedModel(
            [
                asdict(ArrangementPlan(sections=[Section(1, end, LHPattern.BLOCK)])),
                asdict(
                    ArrangementPlan(
                        sections=[Section(1, end, LHPattern.PEDAL_TONE, lh_voices=1)]
                    )
                ),
            ]
        )
        result = arrange_score(
            source,
            profile,
            model,
            max_attempts=request.max_attempts,
            verbose=False,
            countdown=request.countdown,
        )
        return {"result": run_result_to_dict(result)}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.post("/plan/deterministic", response_model=PlanResponse)
def deterministic_plan_endpoint(request: VerifyRequest) -> dict:
    try:
        source = to_score(request.score)
        profile = to_profile(request.profile)
        plan = plan_score(source, profile)
        arranged = render_plan(plan, source)
        verdict = verify_score(arranged, profile)
        fidelity = fidelity_for(source, arranged)
        return {
            "plan": asdict(plan),
            "verdict": verdict_to_dict(verdict),
            "fidelity": fidelity_to_dict(fidelity),
        }
    except Exception as exc:
        raise domain_error(exc) from exc


@router.post("/plan/analysis", response_model=PlanAnalysisResponse)
def plan_analysis_endpoint(request: VerifyRequest) -> dict:
    try:
        source = to_score(request.score)
        profile = to_profile(request.profile)
        return {"analysis": plan_analysis(source, profile)}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.post("/profiles", response_model=RecordResponse)
def create_profile_endpoint(
    request: PlayerProfileIn,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        profile = to_profile(request)
        return {"record": storage.create_profile(user.id, stored_profile_dict(profile))}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.get("/profiles", response_model=RecordsResponse)
def list_profiles_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_profiles(user.id, limit, offset)}


@router.get("/profiles/{profile_id}", response_model=RecordResponse)
def get_profile_endpoint(
    profile_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_profile(user.id, profile_id)
    if record is None:
        raise not_found("profile", profile_id)
    return {"record": record}


@router.put("/profiles/{profile_id}", response_model=RecordResponse)
def update_profile_endpoint(
    profile_id: str,
    request: PlayerProfileIn,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        profile = to_profile(request)
        record = storage.update_profile(user.id, profile_id, stored_profile_dict(profile))
        if record is None:
            raise not_found("profile", profile_id)
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.delete("/profiles/{profile_id}")
def delete_profile_endpoint(
    profile_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not storage.delete_profile(user.id, profile_id):
        raise not_found("profile", profile_id)
    return {"deleted": True}


@router.post("/scores", response_model=RecordResponse)
def create_score_endpoint(
    request: ScoreIn,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        score = to_score(request)
        return {"record": storage.create_score(user.id, stored_score_dict(score))}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.get("/scores", response_model=RecordsResponse)
def list_scores_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_scores(user.id, limit, offset)}


@router.get("/scores/{score_id}", response_model=RecordResponse)
def get_score_endpoint(
    score_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_score(user.id, score_id)
    if record is None:
        raise not_found("score", score_id)
    return {"record": record}


@router.delete("/scores/{score_id}")
def delete_score_endpoint(
    score_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not storage.delete_score(user.id, score_id):
        raise not_found("score", score_id)
    return {"deleted": True}


@router.post("/plans", response_model=RecordResponse)
def create_plan_endpoint(
    request: PlanCreateRequest,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        if storage.get_score(user.id, request.score_id) is None:
            raise not_found("score", request.score_id)
        plan = to_plan(request.plan)
        return {"record": storage.create_plan(user.id, request.score_id, asdict(plan))}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.get("/plans", response_model=RecordsResponse)
def list_plans_endpoint(
    score_id: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_plans(user.id, score_id, limit, offset)}


@router.get("/plans/{plan_id}", response_model=RecordResponse)
def get_plan_endpoint(
    plan_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_plan(user.id, plan_id)
    if record is None:
        raise not_found("plan", plan_id)
    return {"record": record}


@router.put("/plans/{plan_id}", response_model=RecordResponse)
def update_plan_endpoint(
    plan_id: str,
    request: ArrangementPlanIn,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        plan = to_plan(request)
        record = storage.update_plan(user.id, plan_id, asdict(plan))
        if record is None:
            raise not_found("plan", plan_id)
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.delete("/plans/{plan_id}")
def delete_plan_endpoint(
    plan_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not storage.delete_plan(user.id, plan_id):
        raise not_found("plan", plan_id)
    return {"deleted": True}


@router.post("/arrangements/render-and-verify", response_model=RecordResponse)
def create_arrangement_endpoint(
    request: PersistentRenderVerifyRequest,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(require_verified_user),
) -> dict:
    try:
        score_record = storage.get_score(user.id, request.score_id)
        profile_record = storage.get_profile(user.id, request.profile_id)
        plan_record = storage.get_plan(user.id, request.plan_id)
        if score_record is None:
            raise not_found("score", request.score_id)
        if profile_record is None:
            raise not_found("profile", request.profile_id)
        if plan_record is None:
            raise not_found("plan", request.plan_id)
        if plan_record["score_id"] != request.score_id:
            raise conflict(
                "plan_score_mismatch",
                "This plan was written for a different score.",
            )

        source = score_from_payload(score_record["payload"])
        profile = profile_from_payload(profile_record["payload"])
        plan = plan_from_payload(plan_record["payload"])
        arranged = render_plan(plan, source)
        verdict = verify_score(arranged, profile)
        fidelity = fidelity_for(source, arranged)
        record = storage.create_arrangement(
            user_id=user.id,
            score_id=request.score_id,
            plan_id=request.plan_id,
            profile_id=request.profile_id,
            arranged=stored_score_dict(arranged),
            verdict=verdict_to_dict(verdict),
            fidelity=fidelity_to_dict(fidelity),
        )
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.get("/arrangements", response_model=RecordsResponse)
def list_arrangements_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_arrangements(user.id, limit, offset)}


EXPORT_BATCH_SIZE = 100


@router.get("/arrangements/export")
def export_arrangements_endpoint(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """Every saved arrangement of the signed-in user, streamed as NDJSON.

    One JSON object per line, oldest first, in the shape `GET /arrangements`
    returns. The rows are read `EXPORT_BATCH_SIZE` at a time: on Postgres from
    a server-side cursor, on SQLite by iterating the statement. The stream
    holds its own connection for as long as it runs, so the request-scoped
    one is not tied up and nothing is buffered in memory.
    """
    database: Database = request.app.state.database

    def stream():
        conn = database.acquire()
        try:
            storage = Storage(conn, dialect=database.dialect)
            for record in storage.iter_arrangements(user.id, batch_size=EXPORT_BATCH_SIZE):
                yield json.dumps(record, default=str) + "\n"
        finally:
            database.release(conn)

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="arrangements.ndjson"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/arrangements/{arrangement_id}", response_model=RecordResponse)
def get_arrangement_endpoint(
    arrangement_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_arrangement(user.id, arrangement_id)
    if record is None:
        raise not_found("arrangement", arrangement_id)
    return {"record": record}


@router.get("/arrangements/{arrangement_id}/verdict")
def get_arrangement_verdict_endpoint(
    arrangement_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_arrangement(user.id, arrangement_id)
    if record is None:
        raise not_found("arrangement", arrangement_id)
    return record["verdict"]


@router.post("/runs/dry-run", response_model=RecordResponse)
def create_dry_run_endpoint(
    request: PersistentArrangeRequest,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(require_verified_user),
) -> dict:
    try:
        score_record = storage.get_score(user.id, request.score_id)
        profile_record = storage.get_profile(user.id, request.profile_id)
        if score_record is None:
            raise not_found("score", request.score_id)
        if profile_record is None:
            raise not_found("profile", request.profile_id)

        source = score_from_payload(score_record["payload"])
        profile = profile_from_payload(profile_record["payload"])
        end = last_bar(source)
        model = ScriptedModel(
            [
                asdict(ArrangementPlan(sections=[Section(1, end, LHPattern.BLOCK)])),
                asdict(
                    ArrangementPlan(
                        sections=[Section(1, end, LHPattern.PEDAL_TONE, lh_voices=1)]
                    )
                ),
            ]
        )
        result = arrange_score(
            source,
            profile,
            model,
            max_attempts=request.max_attempts,
            verbose=False,
            countdown=request.countdown,
        )
        rankings = plan_analysis(source, profile)["candidate_rankings"]
        with storage.transaction():
            record = storage.create_run(
                user_id=user.id,
                score_id=request.score_id,
                profile_id=request.profile_id,
                result=run_result_to_dict(result),
            )
            storage.create_candidate_rankings(
                user_id=user.id,
                run_id=record["id"],
                score_id=request.score_id,
                profile_id=request.profile_id,
                rows=rankings,
            )
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.get("/candidate-rankings", response_model=RecordsResponse)
def list_candidate_rankings_endpoint(
    run_id: str | None = None,
    score_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {
        "records": storage.list_candidate_rankings(
            user.id,
            run_id=run_id,
            score_id=score_id,
            limit=limit,
            offset=offset,
        )
    }


@router.post("/feedback", response_model=RecordResponse)
def create_feedback_endpoint(
    request: FeedbackCreateRequest,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        edited_plan = None
        if request.edited_plan is not None:
            edited_plan = asdict(to_plan(request.edited_plan))
        record = storage.create_candidate_feedback(
            user_id=user.id,
            candidate_ranking_id=request.candidate_ranking_id,
            arrangement_id=request.arrangement_id,
            label=request.label,
            edited_plan=edited_plan,
            notes=request.notes,
        )
        if record is None:
            raise not_found("feedback target", "requested target")
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@router.get("/feedback", response_model=RecordsResponse)
def list_feedback_endpoint(
    candidate_ranking_id: str | None = None,
    arrangement_id: str | None = None,
    label: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {
        "records": storage.list_candidate_feedback(
            user.id,
            candidate_ranking_id=candidate_ranking_id,
            arrangement_id=arrangement_id,
            label=label,
            limit=limit,
            offset=offset,
        )
    }


@router.get("/runs", response_model=RecordsResponse)
def list_runs_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_runs(user.id, limit, offset)}


@router.get("/runs/{run_id}", response_model=RecordResponse)
def get_run_endpoint(
    run_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_run(user.id, run_id)
    if record is None:
        raise not_found("run", run_id)
    return {"record": record}


# --- application factory -----------------------------------------------------


def build_rate_limit_store(
    settings: Settings, database: Database, metrics: AppMetrics
) -> RateLimitStore:
    """`memory`: per process. `database`: shared by every instance, with the
    in-memory store as a fallback so a database outage degrades rate limiting
    to per-process instead of switching it off or failing requests."""
    memory = InMemoryRateLimitStore(max_buckets=settings.rate_limit_max_buckets)
    if settings.rate_limit_backend != "database":
        return memory

    last_logged = [0.0]

    def on_error(exc: Exception) -> None:
        metrics.rate_limit_store_errors.inc()
        now = time.monotonic()
        if now - last_logged[0] >= 60:
            last_logged[0] = now
            logger.error("rate_limit_store_failed", exc_info=exc)

    return FallbackRateLimitStore(DatabaseRateLimitStore(database), memory, on_error=on_error)


def run_cleanup(app: FastAPI) -> dict[str, int]:
    """Delete dead sessions, spent/expired tokens and closed rate-limit windows."""
    settings: Settings = app.state.settings
    database: Database = app.state.database
    with database.connection() as conn:
        removed = Storage(conn, dialect=database.dialect).cleanup_expired(
            idle_seconds=settings.session_idle_days * 86_400
        )
    removed["rate_limit_buckets"] = app.state.rate_limits.store.cleanup()
    return removed


def _cleanup_loop(app: FastAPI, stop: threading.Event) -> None:
    interval = max(1, app.state.settings.cleanup_interval_seconds)
    while True:
        try:
            removed = run_cleanup(app)
            if any(removed.values()):
                log_event(logger, "cleanup", **removed)
        except Exception as exc:
            logger.error("cleanup_failed", exc_info=exc)
        if stop.wait(interval):
            return


def _schema_is_current(database: Database) -> bool:
    try:
        with database.connection() as conn:
            pending = Storage(conn, dialect=database.dialect).migration_status()["pending"]
            conn.rollback()
        return not pending
    except Exception as exc:
        logger.error("migration_status_failed", exc_info=exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: migrate once (under a database lock), then start housekeeping.

    This is the only place the API applies migrations. With
    `RUN_MIGRATIONS_ON_STARTUP=false` the deploy is expected to have run
    `python -m arranger_api.storage.migrate` as a release step.
    """
    settings: Settings = app.state.settings
    database: Database = app.state.database
    if settings.run_migrations_on_startup:
        applied = await run_in_threadpool(database.migrate)
        log_event(logger, "migrations", applied=applied, dialect=database.dialect)
    else:
        log_event(logger, "migrations_skipped", reason="RUN_MIGRATIONS_ON_STARTUP=false")

    stop = threading.Event()
    worker = threading.Thread(
        target=_cleanup_loop, args=(app, stop), name="arranger-cleanup", daemon=True
    )
    worker.start()
    runner: JobRunner = app.state.job_runner
    if settings.job_workers > 0:
        if await run_in_threadpool(_schema_is_current, database):
            # Jobs left `running` by a process that died are picked up again
            # once their lease expires; start by recovering any that already have.
            await run_in_threadpool(runner.housekeeping)
            runner.start(settings.job_workers)
            log_event(logger, "job_workers_started", workers=settings.job_workers)
        else:
            # Migrations are a release step here and have not run yet. Serving
            # /ready (which says so) matters more than crashing on a missing table.
            logger.error("job_workers_not_started: the database has pending migrations")
    try:
        yield
    finally:
        runner.stop()
        stop.set()
        worker.join(timeout=5)
        database.close()


async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=public_error_body(exc.status_code, exc.detail),
        headers=getattr(exc, "headers", None),
    )


async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    # FastAPI's default body echoes the rejected input, which for the auth
    # endpoints means echoing passwords and tokens. Keep where and why only.
    errors = [
        {
            "type": str(error.get("type", "value_error")),
            "loc": [str(part) if not isinstance(part, int) else part for part in error.get("loc", ())],
            "msg": str(error.get("msg", "Invalid value.")),
        }
        for error in exc.errors()[:50]
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


async def _database_unavailable_handler(request: Request, exc: DatabaseUnavailable):
    logger.error("database_unavailable", exc_info=exc)
    return JSONResponse(
        status_code=503,
        content=public_error_body(503, {"error": "database_unavailable"}),
        headers={"Retry-After": "5"},
    )


async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=public_error_body(500, {"error": "internal_server_error"}),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Raises `ConfigurationError` on unsafe production config."""
    settings = settings or load_settings()
    validate_settings(settings)
    configure_logging(settings.log_level)

    metrics = AppMetrics()
    database = Database.from_settings(settings)
    rate_limits = RateLimits(
        settings,
        build_rate_limit_store(settings, database, metrics),
        on_limited=lambda name: metrics.rate_limit_hits.inc(limiter=name),
    )

    docs = {} if not settings.is_production else {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }
    app = FastAPI(
        title="Arranger API",
        version=__version__,
        description="HTTP interface for rendering and verifying playable piano arrangements.",
        lifespan=lifespan,
        dependencies=[Depends(enforce_rate_limit)],
        **docs,
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.database = database
    app.state.rate_limits = rate_limits
    app.state.passwords = PasswordService.from_settings(settings)
    app.state.email_sender = build_email_sender(settings)
    app.state.email_health = EmailHealth()
    # Files, engraving and background work. All three are replaceable on
    # `app.state` so tests can run without LilyPond, a model or a second thread.
    app.state.artifacts = build_artifact_store(settings, database)
    app.state.engraver = LilyPondEngraver(timeout=float(settings.engrave_max_seconds))
    # None with the default JOB_QUEUE_BACKEND=database: workers poll the table.
    app.state.job_queue = build_job_queue(settings)
    app.state.job_services = JobServices(
        settings=settings, database=database, artifacts=app.state.artifacts, metrics=metrics,
        engraver=app.state.engraver, queue=app.state.job_queue,
    )
    app.state.job_runner = JobRunner(app.state.job_services)

    log_event(logger, "email_config", **email_diagnostics(settings))
    if settings.is_production and settings.trusted_proxy_count == 0:
        logger.warning(
            "TRUSTED_PROXY_COUNT=0: X-Forwarded-For is ignored. Behind a reverse proxy "
            "every client shares the proxy's address and therefore one rate-limit bucket."
        )

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(DatabaseUnavailable, _database_unavailable_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    # add_middleware prepends: the last one added is the outermost.
    app.add_middleware(BodyLimitMiddleware, settings=settings, metrics=metrics)
    app.add_middleware(
        RequestGuardMiddleware, settings=settings, metrics=metrics, rate_limits=rate_limits
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o for o in settings.cors_origins if o not in {"*", "null"}],
        allow_credentials=True,
        allow_methods=CORS_ALLOW_METHODS,
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=CORS_EXPOSE_HEADERS,
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, settings=settings, metrics=metrics)

    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(router)

    from arranger_api.routers import register_routers

    register_routers(app)

    # Last: the catch-all static mount must not shadow any API route.
    if settings.frontend_dir.exists():
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
    return app


app = create_app()
settings = app.state.settings


def main() -> None:
    """Run the development API server."""
    import uvicorn

    uvicorn.run(
        "arranger_api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        # No `Server: uvicorn` header: the software and version behind a site
        # are nobody's business (ZAP reports it as an information leak).
        server_header=False,
    )
