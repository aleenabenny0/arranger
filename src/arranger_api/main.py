"""FastAPI entry point for Arranger."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from typing import Iterator

from fastapi import Cookie, Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import (
    SESSION_COOKIE,
    CurrentUser,
    clear_session_cookie,
    hash_password,
    hash_token,
    set_session_cookie,
    unauthorized,
    verify_password,
)
from arranger.agent import ScriptedModel
from arranger.application import (
    arrange_score,
    fidelity_for,
    render_plan,
    verify_score,
)
from arranger.plan import ArrangementPlan, LHPattern, Section
from arranger.render import last_bar

from .errors import domain_error, not_found
from .schemas import (
    ArrangeRequest,
    ArrangeResponse,
    ArrangementPlanIn,
    LoginRequest,
    PersistentArrangeRequest,
    PersistentRenderVerifyRequest,
    PlanCreateRequest,
    PlayerProfileIn,
    RecordResponse,
    RecordsResponse,
    RegisterRequest,
    RenderRequest,
    RenderResponse,
    RenderVerifyRequest,
    RenderVerifyResponse,
    ScoreIn,
    UserResponse,
    VerifyRequest,
    fidelity_to_dict,
    run_result_to_dict,
    score_to_dict,
    to_plan,
    to_profile,
    to_score,
    verdict_to_dict,
)
from .storage import INTEGRITY_ERRORS, Storage, connect, init_db
from .settings import load_settings


settings = load_settings()


app = FastAPI(
    title="Arranger API",
    version="0.1.0",
    description="HTTP interface for rendering and verifying playable piano arrangements.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_storage() -> Iterator[Storage]:
    conn = connect()
    init_db(conn)
    try:
        yield Storage(conn)
    finally:
        conn.close()


def get_current_user(
    storage: Storage = Depends(get_storage),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> CurrentUser:
    if not session_token:
        raise unauthorized()
    user = storage.user_for_session(hash_token(session_token))
    if user is None:
        raise unauthorized()
    return CurrentUser(
        id=user["id"],
        email=user["email"],
        display_name=user["display_name"],
    )


def user_payload(user: dict | CurrentUser) -> dict:
    if isinstance(user, CurrentUser):
        return {"id": user.id, "email": user.email, "display_name": user.display_name}
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "arranger-api"}


@app.post("/auth/register", response_model=UserResponse)
def register_endpoint(
    request: RegisterRequest,
    response: Response,
    storage: Storage = Depends(get_storage),
) -> dict:
    try:
        display_name = request.display_name or request.email.split("@", 1)[0]
        user = storage.create_user(
            request.email,
            hash_password(request.password),
            display_name,
        )
        token = secrets.token_urlsafe(32)
        storage.create_session(user["id"], hash_token(token), settings.session_days)
        set_session_cookie(
            response,
            token,
            secure=settings.cookie_secure,
            max_age=60 * 60 * 24 * settings.session_days,
        )
        return {"user": user_payload(user)}
    except INTEGRITY_ERRORS as exc:
        raise domain_error(ValueError("email is already registered")) from exc


@app.post("/auth/login", response_model=UserResponse)
def login_endpoint(
    request: LoginRequest,
    response: Response,
    storage: Storage = Depends(get_storage),
) -> dict:
    user = storage.get_user_with_password(request.email)
    if user is None or not verify_password(request.password, user["password_hash"]):
        raise unauthorized()
    token = secrets.token_urlsafe(32)
    storage.create_session(user["id"], hash_token(token), settings.session_days)
    set_session_cookie(
        response,
        token,
        secure=settings.cookie_secure,
        max_age=60 * 60 * 24 * settings.session_days,
    )
    return {"user": user_payload(user)}


@app.post("/auth/logout")
def logout_endpoint(
    response: Response,
    storage: Storage = Depends(get_storage),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    if session_token:
        storage.revoke_session(hash_token(session_token))
    clear_session_cookie(response, secure=settings.cookie_secure)
    return {"logged_out": True}


@app.get("/auth/me", response_model=UserResponse)
def me_endpoint(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"user": user_payload(user)}


@app.post("/verify")
def verify_endpoint(request: VerifyRequest) -> dict:
    try:
        score = to_score(request.score)
        profile = to_profile(request.profile)
        return verdict_to_dict(verify_score(score, profile))
    except Exception as exc:
        raise domain_error(exc) from exc


@app.post("/render", response_model=RenderResponse)
def render_endpoint(request: RenderRequest) -> dict:
    try:
        source = to_score(request.source)
        plan = to_plan(request.plan)
        arranged = render_plan(plan, source)
        return {"score": score_to_dict(arranged)}
    except Exception as exc:
        raise domain_error(exc) from exc


@app.post("/render-and-verify", response_model=RenderVerifyResponse)
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


@app.post("/arrange/dry-run", response_model=ArrangeResponse)
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


@app.post("/profiles", response_model=RecordResponse)
def create_profile_endpoint(
    request: PlayerProfileIn,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        profile = to_profile(request)
        return {"record": storage.create_profile(user.id, asdict(profile))}
    except Exception as exc:
        raise domain_error(exc) from exc


@app.get("/profiles", response_model=RecordsResponse)
def list_profiles_endpoint(
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_profiles(user.id)}


@app.get("/profiles/{profile_id}", response_model=RecordResponse)
def get_profile_endpoint(
    profile_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_profile(user.id, profile_id)
    if record is None:
        raise not_found("profile", profile_id)
    return {"record": record}


@app.put("/profiles/{profile_id}", response_model=RecordResponse)
def update_profile_endpoint(
    profile_id: str,
    request: PlayerProfileIn,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        profile = to_profile(request)
        record = storage.update_profile(user.id, profile_id, asdict(profile))
        if record is None:
            raise not_found("profile", profile_id)
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@app.delete("/profiles/{profile_id}")
def delete_profile_endpoint(
    profile_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not storage.delete_profile(user.id, profile_id):
        raise not_found("profile", profile_id)
    return {"deleted": True}


@app.post("/scores", response_model=RecordResponse)
def create_score_endpoint(
    request: ScoreIn,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        score = to_score(request)
        return {"record": storage.create_score(user.id, score_to_dict(score))}
    except Exception as exc:
        raise domain_error(exc) from exc


@app.get("/scores", response_model=RecordsResponse)
def list_scores_endpoint(
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_scores(user.id)}


@app.get("/scores/{score_id}", response_model=RecordResponse)
def get_score_endpoint(
    score_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_score(user.id, score_id)
    if record is None:
        raise not_found("score", score_id)
    return {"record": record}


@app.delete("/scores/{score_id}")
def delete_score_endpoint(
    score_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not storage.delete_score(user.id, score_id):
        raise not_found("score", score_id)
    return {"deleted": True}


@app.post("/plans", response_model=RecordResponse)
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


@app.get("/plans", response_model=RecordsResponse)
def list_plans_endpoint(
    score_id: str | None = None,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_plans(user.id, score_id)}


@app.get("/plans/{plan_id}", response_model=RecordResponse)
def get_plan_endpoint(
    plan_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_plan(user.id, plan_id)
    if record is None:
        raise not_found("plan", plan_id)
    return {"record": record}


@app.put("/plans/{plan_id}", response_model=RecordResponse)
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


@app.delete("/plans/{plan_id}")
def delete_plan_endpoint(
    plan_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not storage.delete_plan(user.id, plan_id):
        raise not_found("plan", plan_id)
    return {"deleted": True}


@app.post("/arrangements/render-and-verify", response_model=RecordResponse)
def create_arrangement_endpoint(
    request: PersistentRenderVerifyRequest,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
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

        source = to_score(ScoreIn.model_validate(score_record["payload"]))
        profile = to_profile(PlayerProfileIn.model_validate(profile_record["payload"]))
        plan = to_plan(ArrangementPlanIn.model_validate(plan_record["payload"]))
        arranged = render_plan(plan, source)
        verdict = verify_score(arranged, profile)
        fidelity = fidelity_for(source, arranged)
        record = storage.create_arrangement(
            user_id=user.id,
            score_id=request.score_id,
            plan_id=request.plan_id,
            profile_id=request.profile_id,
            arranged=score_to_dict(arranged),
            verdict=verdict_to_dict(verdict),
            fidelity=fidelity_to_dict(fidelity),
        )
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@app.get("/arrangements", response_model=RecordsResponse)
def list_arrangements_endpoint(
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_arrangements(user.id)}


@app.get("/arrangements/{arrangement_id}", response_model=RecordResponse)
def get_arrangement_endpoint(
    arrangement_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_arrangement(user.id, arrangement_id)
    if record is None:
        raise not_found("arrangement", arrangement_id)
    return {"record": record}


@app.get("/arrangements/{arrangement_id}/verdict")
def get_arrangement_verdict_endpoint(
    arrangement_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_arrangement(user.id, arrangement_id)
    if record is None:
        raise not_found("arrangement", arrangement_id)
    return record["verdict"]


@app.post("/runs/dry-run", response_model=RecordResponse)
def create_dry_run_endpoint(
    request: PersistentArrangeRequest,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        score_record = storage.get_score(user.id, request.score_id)
        profile_record = storage.get_profile(user.id, request.profile_id)
        if score_record is None:
            raise not_found("score", request.score_id)
        if profile_record is None:
            raise not_found("profile", request.profile_id)

        source = to_score(ScoreIn.model_validate(score_record["payload"]))
        profile = to_profile(PlayerProfileIn.model_validate(profile_record["payload"]))
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
        record = storage.create_run(
            user_id=user.id,
            score_id=request.score_id,
            profile_id=request.profile_id,
            result=run_result_to_dict(result),
        )
        return {"record": record}
    except Exception as exc:
        raise domain_error(exc) from exc


@app.get("/runs", response_model=RecordsResponse)
def list_runs_endpoint(
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"records": storage.list_runs(user.id)}


@app.get("/runs/{run_id}", response_model=RecordResponse)
def get_run_endpoint(
    run_id: str,
    storage: Storage = Depends(get_storage),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    record = storage.get_run(user.id, run_id)
    if record is None:
        raise not_found("run", run_id)
    return {"record": record}


if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")


def main() -> None:
    """Run the development API server."""
    import uvicorn

    uvicorn.run(
        "arranger_api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
