"""HTTP calls only queue and database services; collection belongs to the worker."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Connection

from app.jobs import repository

from . import discussions
from .rate_limits import charge
from .responses import APIError, response
from .security import client_key, require_admin

router = APIRouter(prefix="/api/v1")

JOB_FIELDS = {
    "job_id",
    "video_id",
    "input_url",
    "requested_at",
    "created_at",
    "hour_bucket",
    "status",
    "phase",
    "requested_mode",
    "effective_mode",
    "progress",
    "requests",
    "max_requests",
    "retry_count",
    "attempt_count",
    "not_before",
    "result_state_id",
    "completed_at",
    "safe_error",
    "result_expired",
    "cancel_requested",
}
REQUEST_FIELDS = {
    "request_id",
    "normalized_url",
    "accepted_at",
    "hour_bucket",
    "status",
    "video_id",
    "job_id",
    "requests",
    "max_requests",
    "retry_count",
    "not_before",
    "completed_at",
    "safe_error",
}


class VideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048, strict=True)


class RefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "full"] = "auto"


def connection(request: Request):
    engine = request.app.state.database_engine
    if engine is None:
        raise APIError("database_not_configured", 503)
    with engine.connect() as conn:
        yield conn


DBConnection = Annotated[Connection, Depends(connection)]
AdminAccess = Annotated[None, Depends(require_admin)]


def write_client(request: Request, *, conn: DBConnection):
    request.state.accepted_at = datetime.now(UTC)
    identity = client_key(request)
    with conn.begin():
        charge(conn, identity, "write", now=request.state.accepted_at)
    return identity


WriteClient = Annotated[str, Depends(write_client)]


def _intent_callback(identity, created, accepted_at):
    def callback(conn):
        charge(conn, identity, "intent", now=accepted_at)
        created.append(True)

    return callback


@router.post("/video-requests")
def submit_video(request: Request, body: VideoInput, identity: WriteClient, *, conn: DBConnection):
    created = []
    result = repository.submit(
        conn,
        body.url,
        now=request.state.accepted_at,
        on_new_intent=_intent_callback(identity, created, request.state.accepted_at),
    )
    return response(result, 202 if created else 200)


@router.get("/video-requests/{request_id}")
def get_request(request_id: UUID, *, conn: DBConnection):
    row = repository.get_request(conn, str(request_id))
    return response({key: row[key] for key in REQUEST_FIELDS if key in row})


@router.get("/videos/{video_id}")
def get_video(video_id: str, *, conn: DBConnection):
    return response(discussions.video_view(conn, video_id))


@router.post("/videos/{video_id}/refresh")
def refresh_video(
    request: Request,
    video_id: str,
    body: RefreshInput,
    identity: WriteClient,
    *,
    conn: DBConnection,
):
    discussions.video_identity(video_id)
    created = []
    result = repository.request_refresh(
        conn,
        video_id,
        now=request.state.accepted_at,
        mode=body.mode,
        on_new_intent=_intent_callback(identity, created, request.state.accepted_at),
    )
    if result.get("refresh_block_reason"):
        return response({"error": "refresh_not_allowed", **result}, 409)
    return response(result, 202 if created else 200)


@router.get("/jobs/{job_id}")
def get_job(job_id: UUID, *, conn: DBConnection):
    row = repository.get_job(conn, str(job_id))
    return response({key: row[key] for key in JOB_FIELDS if key in row})


@router.get("/states/{state_id}/threads")
def get_threads(
    state_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=4096),
    *,
    conn: DBConnection,
):
    return response(discussions.thread_page(conn, str(state_id), limit, cursor))


@router.get("/states/{state_id}/threads/{root_id}/comments")
def get_thread_comments(
    state_id: UUID,
    root_id: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=4096),
    *,
    conn: DBConnection,
):
    return response(discussions.comment_page(conn, str(state_id), root_id, "thread", limit, cursor))


@router.get("/states/{state_id}/users/unknown/comments")
def get_unknown_comments(
    state_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=4096),
    *,
    conn: DBConnection,
):
    return response(discussions.comment_page(conn, str(state_id), None, "user", limit, cursor))


@router.get("/states/{state_id}/users/{uid}/comments")
def get_user_comments(
    state_id: UUID,
    uid: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=4096),
    *,
    conn: DBConnection,
):
    return response(discussions.comment_page(conn, str(state_id), uid, "user", limit, cursor))


@router.post("/admin/jobs/{job_id}/{action}")
def control_job(
    job_id: UUID,
    action: Literal["recover", "retry", "cancel"],
    _admin: AdminAccess,
    _client: WriteClient,
    *,
    conn: DBConnection,
):
    row = repository.control_job(conn, str(job_id), action)
    return response({key: row[key] for key in JOB_FIELDS if key in row}, 202)


@router.post("/admin/video-requests/{request_id}/retry")
def retry_request(
    request_id: UUID, _admin: AdminAccess, _client: WriteClient, *, conn: DBConnection
):
    row = repository.control_request(conn, str(request_id), "retry")
    return response({key: row[key] for key in REQUEST_FIELDS if key in row}, 202)


@router.post("/admin/source/revalidate")
def revalidate(_admin: AdminAccess, _client: WriteClient, *, conn: DBConnection):
    row = repository.request_revalidation(conn)
    return response(
        {
            key: row[key]
            for key in ("source_gate", "blocked_kind", "blocked_id", "safe_error", "action")
        },
        202,
    )
