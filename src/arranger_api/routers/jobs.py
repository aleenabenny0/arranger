"""Jobs as a resource of their own: start an arrangement job by project id.

`POST /jobs/arrange` is the queue-facing twin of `POST /projects/{id}/arrangements`:
the same checks, the same job row, and the project id in the body instead of
the path. When `JOB_QUEUE_BACKEND` is `sqs` or `memory` the job's message is
sent as well, and a worker that receives it runs the job. The answer is 202
with the job; the browser polls `GET /jobs/{id}` until it ends.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from ..auth import CurrentUser
from ..deps import get_settings, rate_limit, require_verified_user
from ..schemas import RecordId
from ..settings import Settings
from ..storage.workspace import Workspace
from .projects import IDEMPOTENCY_HEADER, ArrangeIn, get_workspace, queue_arrangement

router = APIRouter(tags=["jobs"])


class ArrangeJobIn(ArrangeIn):
    project_id: RecordId


@router.post(
    "/jobs/arrange", status_code=202,
    dependencies=[Depends(rate_limit("arrange", 30, 60, per="user"))],
)
def start_arrange_job(
    body: ArrangeJobIn, request: Request,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    user: CurrentUser = Depends(require_verified_user),
    ws: Workspace = Depends(get_workspace),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Queue an arrangement of one of the user's projects. 202 with the job to poll."""
    return queue_arrangement(request, ws, user, settings, body.project_id, body, idempotency_key)
