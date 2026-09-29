"""Your data: see how much you are storing, take a copy, delete everything.

These routes are what make the privacy page true. Anything the policy says a
user can do with their data has to be a route here that actually does it.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from ..artifacts import safe_filename
from ..auth import CurrentUser, clear_session_cookie
from ..deps import get_current_user, get_passwords, get_settings, get_storage, rate_limit
from ..errors import api_error
from ..schemas import MAX_PASSWORD_CHARS, ApiModel
from ..settings import Settings
from ..storage import Storage
from ..storage.workspace import Workspace

router = APIRouter(prefix="/account", tags=["account"])

MAX_EXPORT_BYTES = 200 * 1024 * 1024


class DeleteAccountIn(ApiModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    confirm: str = Field(max_length=32)


@router.get("/usage")
def usage(user: CurrentUser = Depends(get_current_user), storage: Storage = Depends(get_storage),
          settings: Settings = Depends(get_settings)) -> dict:
    ws = Workspace(storage)
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    return {
        "projects": {"used": ws.count_projects(user.id), "limit": settings.quota_projects},
        "storage_bytes": {"used": ws.storage_used(user.id), "limit": settings.quota_storage_bytes},
        "jobs_today": {"used": ws.count_jobs_since(user.id, day_ago), "limit": settings.quota_jobs_per_day},
        "transcriptions_today": {"used": ws.count_jobs_since(user.id, day_ago, "transcribe"),
                                 "limit": settings.quota_transcriptions_per_day},
        "active_jobs": {"used": ws.count_active_jobs(user.id), "limit": settings.quota_active_jobs},
        "retention": {"upload_days": settings.upload_retention_days, "export_days": settings.export_retention_days},
    }


@router.get("/export", dependencies=[Depends(rate_limit("account_export", 3, 3600, per="user"))])
def export_account(request: Request, user: CurrentUser = Depends(get_current_user),
                   storage: Storage = Depends(get_storage)) -> Response:
    """Everything stored about this account, as one zip: JSON records plus every file."""
    ws = Workspace(storage)
    data = ws.export_user(user.id)
    store = request.app.state.artifacts
    buffer = io.BytesIO()
    total = 0
    missing: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for artifact in ws.artifact_keys_for_user(user.id):
            try:
                blob = store.get(artifact["storage_key"])
            except Exception:
                missing.append(artifact["id"])
                continue
            total += len(blob)
            if total > MAX_EXPORT_BYTES:
                raise api_error(413, "export_too_large",
                                "Your files are too large to export in one download. Contact support.")
            name = safe_filename(artifact["filename"], "file")
            archive.writestr(f"files/{artifact['id']}-{name}", blob)
        data["files_not_included"] = missing
        data["exported_at"] = datetime.now(timezone.utc).isoformat()
        data["note"] = ("Password hashes, session tokens and reset tokens are deliberately left out. "
                        "Files that have passed their retention period are listed in files_not_included.")
        archive.writestr("account.json", json.dumps(data, indent=2, default=str))
    return Response(
        content=buffer.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="arranger-account-export.zip"',
                 "X-Content-Type-Options": "nosniff"},
    )


@router.delete("", dependencies=[Depends(rate_limit("account_delete", 5, 3600, per="user"))])
def delete_account(body: DeleteAccountIn, request: Request, response: Response,
                   user: CurrentUser = Depends(get_current_user), storage: Storage = Depends(get_storage),
                   passwords=Depends(get_passwords)) -> dict:
    """Delete the account and everything in it. Needs the password and the word DELETE."""
    if body.confirm != "DELETE":
        raise api_error(400, "confirmation_required", "Type DELETE to confirm.")
    record = storage.get_user_with_password(user.email)
    if record is None or not passwords.verify(body.password, record["password_hash"]).ok:
        raise api_error(403, "wrong_password", "That password is not correct.")
    keys = Workspace(storage).delete_user(user.id)
    store = request.app.state.artifacts
    removed = 0
    for key in keys:
        try:
            store.delete(key)
            removed += 1
        except Exception:
            pass
    clear_session_cookie(response, secure=request.app.state.settings.cookie_secure)
    return {"deleted": True, "files_removed": removed, "files_total": len(keys)}
