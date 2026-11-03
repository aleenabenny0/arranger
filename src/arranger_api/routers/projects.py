"""Projects: import music, correct it, arrange it, review revisions, export files.

Every route resolves its records through `Workspace`, whose queries all carry
the current user's id. A project, source, arrangement, job or artifact that
belongs to someone else is indistinguishable from one that does not exist: 404.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from arranger.adapters.score_json import score_from_dict, score_to_dict
from arranger.application import workflows
from arranger.limits import DEFAULT_LIMITS, ScoreImportError
from arranger.profile import PlayerProfile
from arranger.selection import SelectionError, SourceSelection, apply_selection

from ..artifacts import CONTENT_TYPES, ArtifactNotFound, new_storage_key, safe_filename, sha256_hex
from ..auth import CurrentUser
from ..deps import get_current_user, get_settings, get_storage, rate_limit, require_verified_user
from ..errors import api_error, bad_request, domain_error, not_found
from ..schemas import MAX_LABEL_CHARS, ApiModel, ArrangementPlanIn, PlayerProfileIn, RecordId
from ..settings import Settings
from ..storage import Storage
from ..storage.repositories import format_timestamp
from ..storage.workspace import Workspace

router = APIRouter(tags=["projects"])

IDEMPOTENCY_HEADER = "Idempotency-Key"
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


def get_workspace(storage: Storage = Depends(get_storage)) -> Workspace:
    return Workspace(storage)


# --- request bodies ----------------------------------------------------------------


class ProjectPatch(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_LABEL_CHARS)
    composer: str | None = Field(default=None, max_length=MAX_LABEL_CHARS)


class SourceRevisionIn(ApiModel):
    based_on: RecordId
    selection: dict[str, Any] = Field(default_factory=dict)


class ArrangeIn(ApiModel):
    source_id: RecordId | None = None
    profile: PlayerProfileIn
    use_model: bool = False
    plan: ArrangementPlanIn | None = None     # present: evaluate this edited plan instead of planning
    label: str = Field(default="", max_length=120)


class LabelIn(ApiModel):
    label: str = Field(max_length=120)


# --- helpers -------------------------------------------------------------------------


def _clean_text(value: str, limit: int = MAX_LABEL_CHARS) -> str:
    return "".join(ch for ch in value if ch.isprintable()).strip()[:limit]


def _project_out(project: dict) -> dict:
    return {
        key: project.get(key)
        for key in ("id", "title", "composer", "source_kind", "current_source_id",
                    "current_arrangement_id", "profile", "created_at", "updated_at",
                    "arrangement_count", "note_count", "bar_count")
        if key in project
    }


def _job_out(job: dict) -> dict:
    return {
        "id": job["id"], "kind": job["kind"], "status": job["status"], "project_id": job["project_id"],
        "progress": round(float(job["progress"] or 0), 3), "stage": job["stage"],
        "attempts": job["attempts"], "max_attempts": job["max_attempts"],
        "cancel_requested": job["cancel_requested"], "result": job.get("result"),
        "error": ({"code": job["error_code"], "message": job["error_public"]} if job["error_code"] else None),
        "created_at": job["created_at"], "started_at": job["started_at"], "finished_at": job["finished_at"],
    }


def _require_project(ws: Workspace, user: CurrentUser, project_id: str) -> dict:
    project = ws.get_project(user.id, project_id)
    if project is None:
        raise not_found("Project", project_id)
    return project


def _check_job_quota(ws: Workspace, user: CurrentUser, settings: Settings, kind: str) -> None:
    if ws.count_active_jobs(user.id) >= settings.quota_active_jobs:
        raise api_error(429, "too_many_active_jobs",
                        "You already have the most jobs allowed running at once. Wait for one to finish.")
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    if ws.count_jobs_since(user.id, day_ago) >= settings.quota_jobs_per_day:
        raise api_error(429, "daily_job_quota", "You have reached today's limit for background jobs.")
    if kind == "transcribe" and ws.count_jobs_since(user.id, day_ago, "transcribe") >= settings.quota_transcriptions_per_day:
        raise api_error(429, "daily_transcription_quota", "You have reached today's limit for audio transcription.")


def _idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    import re

    if not re.fullmatch(_IDEMPOTENCY_PATTERN, value):
        raise bad_request("invalid_idempotency_key", "Idempotency-Key must be 8-64 letters, digits, '-' or '_'.")
    return value


def _store_bytes(request: Request, ws: Workspace, user: CurrentUser, *, kind: str, data: bytes, filename: str,
                 project_id: str | None, arrangement_id: str | None = None, retention_days: int = 0) -> dict:
    settings: Settings = request.app.state.settings
    if ws.storage_used(user.id) + len(data) > settings.quota_storage_bytes:
        raise api_error(413, "storage_quota", "Your storage is full. Delete a project to make room.")
    key = new_storage_key(user.id, kind)
    request.app.state.artifacts.put(key, data, CONTENT_TYPES[kind][0])
    expires = None
    if retention_days > 0:
        expires = format_timestamp(datetime.now(timezone.utc) + timedelta(days=retention_days))
    try:
        return ws.create_artifact(
            user.id, project_id=project_id, arrangement_id=arrangement_id, source_id=None, kind=kind,
            filename=filename, content_type=CONTENT_TYPES[kind][0], storage_key=key, size_bytes=len(data),
            sha256=sha256_hex(data), expires_at=expires,
        )
    except Exception:
        request.app.state.artifacts.delete(key)
        raise


# --- import -------------------------------------------------------------------------------


@router.post(
    "/projects/import", status_code=201,
    dependencies=[Depends(rate_limit("project_import", 20, 60, per="user"))],
)
async def import_project(
    request: Request,
    response: Response,
    filename: str = Query(default="", max_length=255),
    title: str = Query(default="", max_length=MAX_LABEL_CHARS),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    user: CurrentUser = Depends(require_verified_user),
    ws: Workspace = Depends(get_workspace),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Upload a file as the request body. MIDI and MusicXML import at once;
    a recording is queued for transcription (HTTP 202)."""
    data = await request.body()   # size is already capped by BodyLimitMiddleware
    if not data:
        raise bad_request("empty_upload", "The upload was empty.")
    if ws.count_projects(user.id) >= settings.quota_projects:
        raise api_error(409, "project_quota", "You have reached the most projects allowed. Delete one first.")
    name = safe_filename(filename, "upload")
    kind = workflows.sniff_kind(data, name)

    if kind == "audio":
        from arranger.adapters.audio import audio_support, probe_audio

        support = audio_support()
        if not support.available:
            raise api_error(501, "audio_unavailable", "")
        try:
            await run_in_threadpool(probe_audio, data, filename=name, limits=DEFAULT_LIMITS)
        except ScoreImportError as exc:
            raise domain_error(exc) from exc
        _check_job_quota(ws, user, settings, "transcribe")
        upload = _store_bytes(request, ws, user, kind="upload", data=data, filename=name, project_id=None,
                              retention_days=settings.upload_retention_days)
        job, _ = ws.create_job(
            user.id, kind="transcribe", idempotency_key=_idempotency_key(idempotency_key),
            payload={"upload_artifact_id": upload["id"], "title": _clean_text(title)},
            max_attempts=1,
        )
        response.status_code = 202
        return {"job": _job_out(job)}

    try:
        imported = await run_in_threadpool(workflows.import_bytes, data, name, DEFAULT_LIMITS)
        score = imported.score
        if _clean_text(title):
            score.title = _clean_text(title)
        inspection = await run_in_threadpool(workflows.inspect_source, score, PlayerProfile())
    except ScoreImportError as exc:
        raise domain_error(exc) from exc

    key = new_storage_key(user.id, "upload")
    if ws.storage_used(user.id) + len(data) > settings.quota_storage_bytes:
        raise api_error(413, "storage_quota", "Your storage is full. Delete a project to make room.")
    request.app.state.artifacts.put(key, data, "application/octet-stream")
    expires = None
    if settings.upload_retention_days > 0:
        expires = format_timestamp(datetime.now(timezone.utc) + timedelta(days=settings.upload_retention_days))
    try:
        project = ws.create_project(
            user.id, title=score.title, composer=score.composer, kind=imported.kind, filename=name,
            score=score_to_dict(score), note_count=len(score.notes), bar_count=score.last_bar(),
            inspection=inspection,
            upload={"kind": "upload", "filename": name, "content_type": "application/octet-stream",
                    "storage_key": key, "size_bytes": len(data), "sha256": sha256_hex(data), "expires_at": expires},
        )
    except Exception:
        request.app.state.artifacts.delete(key)
        raise
    return {"project": _project_out(project), "inspection": inspection}


# --- library ---------------------------------------------------------------------------------


@router.get("/projects")
def list_projects(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    user: CurrentUser = Depends(get_current_user),
    ws: Workspace = Depends(get_workspace),
) -> dict:
    rows, total = ws.list_projects(user.id, query=_clean_text(q, 100), limit=limit, offset=offset)
    return {"projects": [_project_out(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/projects/{project_id}")
def get_project(project_id: RecordId, user: CurrentUser = Depends(get_current_user),
                ws: Workspace = Depends(get_workspace)) -> dict:
    project = _require_project(ws, user, project_id)
    source = ws.get_source(user.id, project["current_source_id"]) if project["current_source_id"] else None
    return {
        "project": _project_out(project),
        "source": None if source is None else {
            "id": source["id"], "revision": source["revision"], "kind": source["kind"],
            "filename": source["filename"], "selection": source["selection"],
            "inspection": source["inspection"], "created_at": source["created_at"],
        },
        "sources": ws.list_sources(user.id, project_id),
        "arrangements": ws.list_arrangements(user.id, project_id),
        "jobs": [_job_out(j) for j in ws.list_jobs(user.id, project_id=project_id, active_only=True)],
        "artifacts": ws.list_artifacts(user.id, project_id),
    }


@router.patch("/projects/{project_id}")
def patch_project(project_id: RecordId, body: ProjectPatch, user: CurrentUser = Depends(get_current_user),
                  ws: Workspace = Depends(get_workspace)) -> dict:
    title = _clean_text(body.title) if body.title is not None else None
    if body.title is not None and not title:
        raise bad_request("invalid_title", "A project needs a title.")
    project = ws.update_project(user.id, project_id, title=title,
                                composer=None if body.composer is None else _clean_text(body.composer))
    if project is None:
        raise not_found("Project", project_id)
    return {"project": _project_out(project)}


@router.delete("/projects/{project_id}")
def delete_project(project_id: RecordId, request: Request, user: CurrentUser = Depends(get_current_user),
                   ws: Workspace = Depends(get_workspace)) -> dict:
    keys = ws.delete_project(user.id, project_id)
    if keys is None:
        raise not_found("Project", project_id)
    store = request.app.state.artifacts
    orphaned = 0
    for key in keys:
        try:
            store.delete(key)
        except Exception:
            orphaned += 1   # rows are gone; a sweeper can find unreferenced keys later
    return {"deleted": True, "files_removed": len(keys) - orphaned}


# --- sources -------------------------------------------------------------------------------------


@router.get("/projects/{project_id}/sources/{source_id}")
def get_source(project_id: RecordId, source_id: RecordId, user: CurrentUser = Depends(get_current_user),
               ws: Workspace = Depends(get_workspace)) -> dict:
    source = ws.get_source(user.id, source_id)
    if source is None or source["project_id"] != project_id:
        raise not_found("Source", source_id)
    try:
        score = score_from_dict(source["score"])
        prepared, changes = apply_selection(score, SourceSelection.from_dict(source["selection"]))
    except (ScoreImportError, SelectionError) as exc:
        raise domain_error(exc) from exc
    return {
        "source": {k: source[k] for k in ("id", "project_id", "revision", "kind", "filename", "note_count",
                                           "bar_count", "selection", "inspection", "created_at")},
        "changes": changes,
        "playback": workflows.playback_events(prepared),
    }


@router.get("/projects/{project_id}/sources/{source_id}/musicxml")
def get_source_musicxml(project_id: RecordId, source_id: RecordId, user: CurrentUser = Depends(get_current_user),
                        ws: Workspace = Depends(get_workspace)) -> Response:
    source = ws.get_source(user.id, source_id)
    if source is None or source["project_id"] != project_id:
        raise not_found("Source", source_id)
    try:
        prepared, _ = apply_selection(score_from_dict(source["score"]), SourceSelection.from_dict(source["selection"]))
        xml, _warnings = workflows.export_musicxml(prepared)
    except (ScoreImportError, SelectionError) as exc:
        raise domain_error(exc) from exc
    return Response(content=xml, media_type=CONTENT_TYPES["musicxml"][0])


@router.post("/projects/{project_id}/sources", status_code=201)
def add_source_revision(project_id: RecordId, body: SourceRevisionIn,
                        user: CurrentUser = Depends(get_current_user),
                        ws: Workspace = Depends(get_workspace)) -> dict:
    """Save corrections to the source as a new revision. The import itself is never edited."""
    _require_project(ws, user, project_id)
    previous = ws.get_source(user.id, body.based_on)
    if previous is None or previous["project_id"] != project_id:
        raise not_found("Source", body.based_on)
    try:
        selection = SourceSelection.from_dict(body.selection)
        prepared, _ = apply_selection(score_from_dict(previous["score"]), selection)
        inspection = workflows.inspect_source(prepared, PlayerProfile())
    except SelectionError as exc:
        raise bad_request("invalid_selection", str(exc)) from exc
    except ScoreImportError as exc:
        raise domain_error(exc) from exc
    if isinstance(previous["inspection"], dict) and "transcription" in previous["inspection"]:
        inspection["transcription"] = previous["inspection"]["transcription"]
    source = ws.add_source_revision(
        user.id, project_id, based_on=previous["id"], selection=selection.to_dict(), inspection=inspection,
        note_count=len(prepared.notes), bar_count=prepared.last_bar(),
    )
    return {"source": {k: source[k] for k in ("id", "revision", "selection", "inspection", "created_at")}}


# --- arrangements -----------------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/arrangements", status_code=202,
    dependencies=[Depends(rate_limit("arrange", 30, 60, per="user"))],
)
def start_arrangement(
    project_id: RecordId, body: ArrangeIn,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    user: CurrentUser = Depends(require_verified_user),
    ws: Workspace = Depends(get_workspace),
    settings: Settings = Depends(get_settings),
) -> dict:
    project = _require_project(ws, user, project_id)
    source_id = body.source_id or project["current_source_id"]
    source = ws.get_source(user.id, source_id) if source_id else None
    if source is None or source["project_id"] != project_id:
        raise not_found("Source", source_id or "current")
    try:
        profile = PlayerProfile.from_dict(body.profile.model_dump())
    except ValueError as exc:
        raise bad_request("invalid_profile", str(exc)) from exc
    key = _idempotency_key(idempotency_key)
    # A retried request with the same key gets the job it already started. It
    # must not be refused by a quota that the original request counted against.
    if key is None or ws.job_for_key(user.id, key) is None:
        _check_job_quota(ws, user, settings, "arrange")
    job, created = ws.create_job(
        user.id, kind="arrange", project_id=project_id, idempotency_key=key,
        max_attempts=settings.job_max_attempts,
        payload={
            "project_id": project_id, "source_id": source["id"], "profile": profile.to_dict(),
            "use_model": bool(body.use_model and settings.model_repair_enabled),
            "plan": None if body.plan is None else body.plan.model_dump(), "label": _clean_text(body.label, 120),
        },
    )
    return {"job": _job_out(job), "created": created}


@router.get("/projects/{project_id}/arrangements/{arrangement_id}")
def get_arrangement(project_id: RecordId, arrangement_id: RecordId,
                    user: CurrentUser = Depends(get_current_user),
                    ws: Workspace = Depends(get_workspace)) -> dict:
    arrangement = ws.get_arrangement(user.id, arrangement_id)
    if arrangement is None or arrangement["project_id"] != project_id:
        raise not_found("Arrangement", arrangement_id)
    try:
        arranged = score_from_dict(arrangement["arranged"])
    except ScoreImportError as exc:
        raise domain_error(exc) from exc
    artifacts = [a for a in ws.list_artifacts(user.id, project_id) if a["arrangement_id"] == arrangement_id]
    return {
        "arrangement": {k: arrangement[k] for k in (
            "id", "project_id", "revision", "source_id", "label", "origin", "accepted", "n_hard", "n_strain",
            "fidelity_score", "difficulty", "algorithm_version", "model", "profile", "plan", "verdict",
            "summary", "report", "created_at")},
        "playback": workflows.playback_events(arranged),
        "artifacts": artifacts,
    }


@router.patch("/projects/{project_id}/arrangements/{arrangement_id}")
def label_arrangement(project_id: RecordId, arrangement_id: RecordId, body: LabelIn,
                      user: CurrentUser = Depends(get_current_user),
                      ws: Workspace = Depends(get_workspace)) -> dict:
    existing = ws.get_arrangement(user.id, arrangement_id)
    if existing is None or existing["project_id"] != project_id:
        raise not_found("Arrangement", arrangement_id)
    updated = ws.label_arrangement(user.id, arrangement_id, _clean_text(body.label, 120))
    return {"arrangement": {"id": updated["id"], "label": updated["label"]}}


@router.get("/projects/{project_id}/arrangements/{arrangement_id}/musicxml")
def get_arrangement_musicxml(project_id: RecordId, arrangement_id: RecordId, request: Request,
                             user: CurrentUser = Depends(get_current_user),
                             ws: Workspace = Depends(get_workspace)) -> Response:
    """The MusicXML the notation preview renders. Same bytes as the download."""
    arrangement = ws.get_arrangement(user.id, arrangement_id)
    if arrangement is None or arrangement["project_id"] != project_id:
        raise not_found("Arrangement", arrangement_id)
    artifact = ws.find_artifact(user.id, arrangement_id, "musicxml")
    if artifact is not None:
        try:
            return Response(content=request.app.state.artifacts.get(artifact["storage_key"]),
                            media_type=CONTENT_TYPES["musicxml"][0])
        except ArtifactNotFound:
            pass   # expired by retention: rebuild it from the saved arrangement
    try:
        xml, _ = workflows.export_musicxml(score_from_dict(arrangement["arranged"]))
    except ScoreImportError as exc:
        raise domain_error(exc) from exc
    return Response(content=xml, media_type=CONTENT_TYPES["musicxml"][0])


@router.post("/projects/{project_id}/arrangements/{arrangement_id}/exports/{kind}")
def export_arrangement(
    project_id: RecordId, arrangement_id: RecordId, kind: Literal["midi", "musicxml", "mxl", "pdf"],
    request: Request, response: Response,
    paper: Literal["a4", "letter"] = Query(default="a4"),
    user: CurrentUser = Depends(require_verified_user),
    ws: Workspace = Depends(get_workspace),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Get a downloadable file. MIDI and MusicXML are made at once; a PDF is engraved by a job."""
    arrangement = ws.get_arrangement(user.id, arrangement_id)
    if arrangement is None or arrangement["project_id"] != project_id:
        raise not_found("Arrangement", arrangement_id)
    store = request.app.state.artifacts
    existing = ws.find_artifact(user.id, arrangement_id, kind)
    if existing is not None and store.exists(existing["storage_key"]):
        return {"artifact": {k: existing[k] for k in ("id", "kind", "filename", "size_bytes", "created_at")}}

    if kind == "pdf":
        engraver = request.app.state.engraver
        if not engraver.status().available:
            raise api_error(501, "engraver_unavailable", "")
        _check_job_quota(ws, user, settings, "engrave")
        job, _ = ws.create_job(
            user.id, kind="engrave", project_id=project_id, max_attempts=settings.job_max_attempts,
            idempotency_key=f"engrave-{arrangement_id}-{paper}"[:64],
            payload={"arrangement_id": arrangement_id, "paper": paper},
        )
        if job["status"] in ("failed", "cancelled"):
            job = ws.retry_job(user.id, job["id"]) or job
        response.status_code = 202
        return {"job": _job_out(job)}

    try:
        score = score_from_dict(arrangement["arranged"])
        if kind == "midi":
            data = workflows.export_midi(score)
        elif kind == "mxl":
            from arranger.adapters.musicxml_writer import write_mxl

            data, _ = write_mxl(score)
        else:
            data, _ = workflows.export_musicxml(score)
    except ScoreImportError as exc:
        raise domain_error(exc) from exc
    project = _require_project(ws, user, project_id)
    base = safe_filename(f"{project['title']} - arrangement {arrangement['revision']}", "arrangement")
    artifact = _store_bytes(request, ws, user, kind=kind, data=data, filename=f"{base}.{CONTENT_TYPES[kind][1]}",
                            project_id=project_id, arrangement_id=arrangement_id,
                            retention_days=settings.export_retention_days)
    return {"artifact": {k: artifact[k] for k in ("id", "kind", "filename", "size_bytes", "created_at")}}


@router.get("/projects/{project_id}/compare")
def compare_arrangements(project_id: RecordId, a: RecordId, b: RecordId,
                         user: CurrentUser = Depends(get_current_user),
                         ws: Workspace = Depends(get_workspace)) -> dict:
    """Two revisions side by side: what changed in the plan and in the outcome."""
    first, second = ws.get_arrangement(user.id, a), ws.get_arrangement(user.id, b)
    for record, record_id in ((first, a), (second, b)):
        if record is None or record["project_id"] != project_id:
            raise not_found("Arrangement", record_id)

    def outcome(r: dict) -> dict:
        return {"revision": r["revision"], "label": r["label"], "origin": r["origin"], "accepted": r["accepted"],
                "hard": r["n_hard"], "strain": r["n_strain"], "fidelity": r["fidelity_score"],
                "difficulty": r["difficulty"], "tempo_scale": r["plan"].get("tempo_scale", 1.0),
                "skill_level": r["profile"].get("skill_level"), "created_at": r["created_at"]}

    plan_changes = []
    sections_a, sections_b = first["plan"].get("sections", []), second["plan"].get("sections", [])
    if len(sections_a) != len(sections_b):
        plan_changes.append(f"Sections: {len(sections_a)} became {len(sections_b)}.")
    else:
        for index, (sa, sb) in enumerate(zip(sections_a, sections_b, strict=True)):
            for field in sorted(set(sa) | set(sb)):
                if field != "label" and sa.get(field) != sb.get(field):
                    plan_changes.append(
                        f"Bars {sb.get('start_bar')}-{sb.get('end_bar')}: {field.replace('_', ' ')} "
                        f"{sa.get(field)!r} became {sb.get(field)!r}."
                    )
            if index > 40:
                break
    profile_changes = [
        f"{key.replace('_', ' ')}: {first['profile'].get(key)!r} became {second['profile'].get(key)!r}"
        for key in sorted(set(first["profile"]) | set(second["profile"]))
        if first["profile"].get(key) != second["profile"].get(key)
    ]
    return {"a": outcome(first), "b": outcome(second), "plan_changes": plan_changes[:100],
            "profile_changes": profile_changes, "same_source": first["source_id"] == second["source_id"]}


# --- jobs ------------------------------------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(project_id: RecordId | None = None, active: bool = False,
              user: CurrentUser = Depends(get_current_user), ws: Workspace = Depends(get_workspace)) -> dict:
    return {"jobs": [_job_out(j) for j in ws.list_jobs(user.id, project_id=project_id, active_only=active)]}


@router.get("/jobs/{job_id}")
def get_job(job_id: RecordId, user: CurrentUser = Depends(get_current_user),
            ws: Workspace = Depends(get_workspace)) -> dict:
    job = ws.get_job(user.id, job_id)
    if job is None:
        raise not_found("Job", job_id)
    return {"job": _job_out(job)}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: RecordId, user: CurrentUser = Depends(get_current_user),
               ws: Workspace = Depends(get_workspace)) -> dict:
    job = ws.request_cancel(user.id, job_id)
    if job is None:
        raise not_found("Job", job_id)
    return {"job": _job_out(job)}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: RecordId, user: CurrentUser = Depends(require_verified_user),
              ws: Workspace = Depends(get_workspace), settings: Settings = Depends(get_settings)) -> dict:
    existing = ws.get_job(user.id, job_id)
    if existing is None:
        raise not_found("Job", job_id)
    if existing["status"] not in ("failed", "cancelled"):
        raise api_error(409, "job_not_retryable", "Only a failed or cancelled job can be retried.")
    _check_job_quota(ws, user, settings, existing["kind"])
    return {"job": _job_out(ws.retry_job(user.id, job_id))}


# --- files ---------------------------------------------------------------------------------------------------


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: RecordId, request: Request, user: CurrentUser = Depends(get_current_user),
                      ws: Workspace = Depends(get_workspace)) -> Response:
    """Authenticated streaming. There are no public file URLs to leak or guess."""
    artifact = ws.get_artifact(user.id, artifact_id)
    if artifact is None:
        raise not_found("File", artifact_id)
    try:
        data = request.app.state.artifacts.get(artifact["storage_key"])
    except ArtifactNotFound:
        raise api_error(410, "file_expired", "This file is no longer stored. Export it again.") from None
    filename = safe_filename(artifact["filename"], "download")
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    from urllib.parse import quote

    return Response(
        content=data, media_type=artifact["content_type"],
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
