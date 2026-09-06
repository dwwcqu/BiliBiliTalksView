"""Short-transaction queue submission, reads, resolution, and control intents."""

import re
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.storage.schema import discussion_states, video_links, videos

from .errors import JobError
from .schema import collection_jobs, resolution_requests, source_runtime

QUEUE_CAPACITY = 20
ACTIVE_JOBS = ("queued", "running", "waiting_source", "blocked")
TERMINAL_JOBS = ("succeeded", "partial", "failed", "cancelled")
ACTIVE_REQUESTS = ("queued", "resolving", "waiting_source", "blocked")
_VIDEO = re.compile(r"/video/(BV[0-9A-Za-z]{10})/?")
_EPISODE = re.compile(r"/bangumi/play/ep([1-9][0-9]*)/?")


def _now(value):
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    return value.astimezone(UTC)


def normalize_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise JobError("invalid_url")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.bilibili.com"
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.fragment
        ):
            raise JobError("invalid_url")
    except ValueError:
        raise JobError("invalid_url") from None
    match = _VIDEO.fullmatch(parsed.path) or _EPISODE.fullmatch(parsed.path)
    if match is None:
        raise JobError("invalid_url")
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", "www.bilibili.com", path, "", ""))


def hour_bucket(now: datetime) -> datetime:
    stamp = (
        _now(now)
        .astimezone(timezone(timedelta(hours=8)))
        .replace(minute=0, second=0, microsecond=0)
    )
    return stamp.astimezone(UTC)


def _transaction(conn):
    if conn.in_transaction():
        raise JobError("state_conflict")
    return conn.begin()


def _row(conn, table, column, value, *, lock=False):
    query = select(table).where(column == value)
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).mappings().one_or_none()
    return dict(row) if row else None


def _visible(conn, video):
    ids = [value for value in (video["current_state_id"], video["working_state_id"]) if value]
    rows = (
        {
            str(row["state_id"]): dict(row)
            for row in conn.execute(
                select(discussion_states).where(
                    discussion_states.c.state_id.in_(ids),
                    discussion_states.c.lifecycle.in_(["current", "partial"]),
                )
            ).mappings()
        }
        if ids
        else {}
    )
    current = rows.get(str(video["current_state_id"])) if video["current_state_id"] else None
    partial = rows.get(str(video["working_state_id"])) if video["working_state_id"] else None
    return current, partial


def _active_job(conn, video_id):
    row = (
        conn.execute(
            select(collection_jobs).where(
                collection_jobs.c.video_id == video_id, collection_jobs.c.status.in_(ACTIVE_JOBS)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _queue_lock(conn):
    # A separate transaction mutex avoids runtime-row/video-row lock inversion
    # with the atomic publisher; it is never held during source requests.
    conn.execute(text("SELECT pg_advisory_xact_lock(742915830117)"))


def _capacity(conn):
    count = conn.scalar(
        select(func.count())
        .select_from(collection_jobs)
        .where(collection_jobs.c.status.in_(ACTIVE_JOBS))
    )
    count += conn.scalar(
        select(func.count())
        .select_from(resolution_requests)
        .where(resolution_requests.c.status.in_(ACTIVE_REQUESTS))
    )
    if count >= QUEUE_CAPACITY:
        raise JobError("queue_full")


def _create_job(conn, video_id, input_url, requested_at, bucket, mode="auto", on_new_intent=None):
    _capacity(conn)
    if on_new_intent is not None:
        on_new_intent(conn)
    job_id = str(uuid4())
    conn.execute(
        update(videos).where(videos.c.video_id == video_id).values(collection_requested=True)
    )
    conn.execute(
        insert(collection_jobs).values(
            job_id=job_id,
            video_id=video_id,
            input_url=input_url,
            requested_at=requested_at,
            created_at=_now(None),
            hour_bucket=bucket,
            status="queued",
            requested_mode=mode,
        )
    )
    return _row(conn, collection_jobs, collection_jobs.c.job_id, job_id)


def submit(conn, url, now=None, *, on_new_intent=None):
    stamp, normalized = _now(now), normalize_url(url)
    bucket = hour_bucket(stamp)
    with _transaction(conn):
        _queue_lock(conn)
        video_id = conn.scalar(
            select(video_links.c.video_id).where(video_links.c.normalized_url == normalized)
        )
        if video_id:
            video = _row(conn, videos, videos.c.video_id, video_id, lock=True)
            current, partial = _visible(conn, video)
            active = _active_job(conn, video_id)
            if current or partial:
                return {
                    "disposition": "cache",
                    "video_id": video_id,
                    "state_id": (current or partial)["state_id"],
                    "job_id": active["job_id"] if active else None,
                }
            if active:
                return {"disposition": "job", "video_id": video_id, "job_id": active["job_id"]}
            existing = (
                conn.execute(
                    select(collection_jobs)
                    .where(collection_jobs.c.video_id == video_id)
                    .order_by(collection_jobs.c.requested_at.desc())
                )
                .mappings()
                .first()
            )
            if existing:
                return {"disposition": "job", "video_id": video_id, "job_id": existing["job_id"]}
            if video["collection_requested"]:
                return {
                    "disposition": "refresh_required",
                    "video_id": video_id,
                    "state_id": None,
                    "job_id": None,
                }
            job = _create_job(conn, video_id, normalized, stamp, bucket, on_new_intent=on_new_intent)
            return {"disposition": "job", "video_id": video_id, "job_id": job["job_id"]}
        existing = (
            conn.execute(
                select(resolution_requests).where(
                    resolution_requests.c.normalized_url == normalized,
                    resolution_requests.c.hour_bucket == bucket,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing:
            return {"disposition": "request", "request_id": existing["request_id"]}
        _capacity(conn)
        if on_new_intent is not None:
            on_new_intent(conn)
        request_id = str(uuid4())
        conn.execute(
            insert(resolution_requests).values(
                request_id=request_id,
                normalized_url=normalized,
                accepted_at=stamp,
                hour_bucket=bucket,
                status="queued",
            )
        )
        return {"disposition": "request", "request_id": request_id}


def request_refresh(conn, video_id, now=None, mode="auto", *, on_new_intent=None):
    if mode not in {"auto", "full"}:
        raise JobError("state_conflict")
    stamp = _now(now)
    bucket = hour_bucket(stamp)
    with _transaction(conn):
        _queue_lock(conn)
        video = _row(conn, videos, videos.c.video_id, video_id, lock=True)
        if not video:
            raise JobError("video_not_found")
        active = _active_job(conn, video_id)
        if active:
            return {"disposition": "job", "video_id": video_id, "job_id": active["job_id"]}
        same = (
            conn.execute(
                select(collection_jobs).where(
                    collection_jobs.c.video_id == video_id, collection_jobs.c.hour_bucket == bucket
                )
            )
            .mappings()
            .one_or_none()
        )
        if same:
            return {"disposition": "job", "video_id": video_id, "job_id": same["job_id"]}
        current, partial = _visible(conn, video)
        visible = current or partial
        if any(state and state["hour_bucket"] >= bucket for state in (current, partial)):
            return {
                "disposition": "cache",
                "video_id": video_id,
                "state_id": visible["state_id"],
                "refresh_block_reason": "fresh_cache",
            }
        input_url = conn.scalar(
            select(video_links.c.normalized_url).where(video_links.c.video_id == video_id).limit(1)
        )
        job = _create_job(conn, video_id, input_url or "", stamp, bucket, mode, on_new_intent=on_new_intent)
        return {"disposition": "job", "video_id": video_id, "job_id": job["job_id"]}


def video_view(conn, video_id, now=None):
    stamp = _now(now)
    bucket = hour_bucket(stamp)
    from app.storage.queries import read_snapshot

    with read_snapshot(conn):
        video = _row(conn, videos, videos.c.video_id, video_id)
        if not video:
            raise JobError("video_not_found")
        current, partial = _visible(conn, video)
        active = _active_job(conn, video_id)
        same = (
            conn.execute(
                select(collection_jobs).where(
                    collection_jobs.c.video_id == video_id, collection_jobs.c.hour_bucket == bucket
                )
            )
            .mappings()
            .first()
        )
        fresh = any(state and state["hour_bucket"] >= bucket for state in (current, partial))
        block = (
            "active_job" if active else "hour_used" if same else "fresh_cache" if fresh else None
        )
        next_hour = bucket + timedelta(hours=1) if not active and (same or fresh) else None
        return {
            "video_id": video_id,
            "state_id": current["state_id"]
            if current
            else partial["state_id"]
            if partial
            else None,
            "partial_state_id": partial["state_id"] if partial else None,
            "active_job_id": active["job_id"] if active else None,
            "can_refresh": block is None,
            "block_reason": block,
            "refresh_block_reason": block,
            "next_refresh_at": next_hour,
            "cache_version": video["cache_version"],
        }


def get_job(conn, job_id):
    with _transaction(conn):
        row = _row(conn, collection_jobs, collection_jobs.c.job_id, job_id)
        if not row:
            raise JobError("job_not_found")
        row["result_expired"] = (
            row["status"] in {"succeeded", "partial"} and row["result_state_id"] is None
        )
        return row


def get_request(conn, request_id):
    with _transaction(conn):
        row = _row(conn, resolution_requests, resolution_requests.c.request_id, request_id)
        if not row:
            raise JobError("request_not_found")
        return row


def resolve_request(conn, request_id, owner_token, resolved, now=None, *, commit_guard=None):
    if not callable(commit_guard):
        raise JobError("commit_guard_required")
    stamp = _now(now)
    source = resolved.get("source", {})
    if source.get("platform") != "bilibili" or source.get("comment_type") != 1:
        raise JobError("state_conflict")
    aid, oid = str(source.get("aid", "")), str(source.get("oid", ""))
    if not aid.isdigit() or aid == "0" or oid != aid:
        raise JobError("state_conflict")
    video_id = f"bilibili:video:{aid}"
    with _transaction(conn):
        _queue_lock(conn)
        request = _row(
            conn, resolution_requests, resolution_requests.c.request_id, request_id, lock=True
        )
        if not request:
            raise JobError("request_not_found")
        if (
            request["status"] != "resolving"
            or str(request["owner_token"]) != str(owner_token)
            or not request["lease_until"]
            or request["lease_until"] <= stamp
        ):
            raise JobError("state_conflict")
        conn.execute(
            pg_insert(videos)
            .values(
                video_id=video_id,
                platform="bilibili",
                aid=aid,
                oid=oid,
                comment_type=1,
                bvid=source.get("bvid"),
                episode_id=str(source["episode_id"]) if source.get("episode_id") else None,
            )
            .on_conflict_do_update(
                index_elements=[videos.c.video_id],
                set_={
                    "bvid": source.get("bvid"),
                    "episode_id": str(source["episode_id"]) if source.get("episode_id") else None,
                },
            )
        )
        aliases = {request["normalized_url"], normalize_url(resolved["canonical_url"])}
        if source.get("bvid"):
            aliases.add(normalize_url(f"https://www.bilibili.com/video/{source['bvid']}"))
        if source.get("episode_id"):
            aliases.add(
                normalize_url(f"https://www.bilibili.com/bangumi/play/ep{source['episode_id']}")
            )
        for alias in aliases:
            conn.execute(
                pg_insert(video_links)
                .values(normalized_url=alias, video_id=video_id)
                .on_conflict_do_nothing(index_elements=[video_links.c.normalized_url])
            )
            if (
                conn.scalar(
                    select(video_links.c.video_id).where(video_links.c.normalized_url == alias)
                )
                != video_id
            ):
                raise JobError("video_link_conflict")
        video = _row(conn, videos, videos.c.video_id, video_id, lock=True)
        current, partial = _visible(conn, video)
        job = _active_job(conn, video_id)
        if commit_guard(conn) is False:
            raise JobError("commit_guard_rejected")
        # Resolving this intent into a job replaces its capacity slot.
        conn.execute(
            update(resolution_requests)
            .where(resolution_requests.c.request_id == request_id)
            .values(status="ready")
        )
        if not (current or partial or job):
            prior = (
                conn.execute(
                    select(collection_jobs)
                    .where(collection_jobs.c.video_id == video_id)
                    .order_by(collection_jobs.c.requested_at.desc())
                )
                .mappings()
                .first()
            )
            job = dict(prior) if prior else None
            if job is None and not video["collection_requested"]:
                job = _create_job(
                    conn,
                    video_id,
                    request["normalized_url"],
                    request["accepted_at"],
                    request["hour_bucket"],
                )
        values = {
            "status": "ready",
            "video_id": video_id,
            "job_id": job["job_id"] if job else None,
            "completed_at": stamp,
            "owner_token": None,
            "lease_until": None,
            "heartbeat_at": None,
        }
        conn.execute(
            update(resolution_requests)
            .where(resolution_requests.c.request_id == request_id)
            .values(**values)
        )
        runtime = _row(conn, source_runtime, source_runtime.c.id, 1, lock=True)
        if (
            runtime["source_gate"] == "recovering"
            and runtime["blocked_kind"] == "resolution"
            and str(runtime["blocked_id"]) == str(request_id)
        ):
            conn.execute(
                update(source_runtime)
                .where(source_runtime.c.id == 1)
                .values(
                    source_gate="normal",
                    blocked_kind=None,
                    blocked_id=None,
                    safe_error=None,
                    blocked_at=None,
                    action=None,
                    updated_at=stamp,
                )
            )
        return {
            "disposition": "cache" if current or partial else "job" if job else "refresh_required",
            "request_id": request_id,
            "video_id": video_id,
            "state_id": (current or partial)["state_id"] if current or partial else None,
            "job_id": job["job_id"] if job else None,
        }


def control_job(conn, job_id, action):
    if action not in {"cancel", "retry", "recover"}:
        raise JobError("state_conflict")
    with _transaction(conn):
        _queue_lock(conn)
        row = _row(conn, collection_jobs, collection_jobs.c.job_id, job_id, lock=True)
        if not row:
            raise JobError("job_not_found")
        if action == "cancel":
            if row["status"] in {"succeeded", "partial", "cancelled"}:
                raise JobError("state_conflict")
            values = (
                {"cancel_requested": True}
                if row["status"] == "running"
                else {
                    "status": "cancelled",
                    "cancel_requested": True,
                    "completed_at": datetime.now(UTC),
                }
            )
        elif action == "retry":
            if row["progress"].get("work_expired"):
                raise JobError("work_expired")
            if row["status"] != "failed":
                raise JobError("state_conflict")
            if row["requests"] >= row["max_requests"]:
                raise JobError("budget_exhausted")
            if _active_job(conn, row["video_id"]):
                raise JobError("state_conflict")
            _capacity(conn)
            values = {
                "status": "queued",
                "phase": "queued",
                "action": "retry",
                "not_before": None,
                "completed_at": None,
                "safe_error": None,
                "owner_token": None,
                "lease_until": None,
                "heartbeat_at": None,
            }
        else:
            if row["status"] != "blocked":
                raise JobError("state_conflict")
            values = {"action": "recover"}
        conn.execute(
            update(collection_jobs).where(collection_jobs.c.job_id == job_id).values(**values)
        )
        return _row(conn, collection_jobs, collection_jobs.c.job_id, job_id)


def control_request(conn, request_id, action):
    if action not in {"retry", "recover"}:
        raise JobError("state_conflict")
    with _transaction(conn):
        _queue_lock(conn)
        row = _row(
            conn, resolution_requests, resolution_requests.c.request_id, request_id, lock=True
        )
        if not row:
            raise JobError("request_not_found")
        expected = "failed" if action == "retry" else "blocked"
        if row["status"] != expected:
            raise JobError("state_conflict")
        if action == "retry" and row["requests"] >= row["max_requests"]:
            raise JobError("budget_exhausted")
        if action == "retry":
            _capacity(conn)
        values = (
            {
                "status": "queued",
                "action": "retry",
                "not_before": None,
                "completed_at": None,
                "safe_error": None,
                "owner_token": None,
                "lease_until": None,
                "heartbeat_at": None,
            }
            if action == "retry"
            else {"action": "recover"}
        )
        conn.execute(
            update(resolution_requests)
            .where(resolution_requests.c.request_id == request_id)
            .values(**values)
        )
        return _row(conn, resolution_requests, resolution_requests.c.request_id, request_id)


def request_revalidation(conn):
    with _transaction(conn):
        runtime = _row(conn, source_runtime, source_runtime.c.id, 1, lock=True)
        if not runtime or runtime["blocked_id"] is None:
            raise JobError("state_conflict")
        conn.execute(
            update(source_runtime)
            .where(source_runtime.c.id == 1)
            .values(action="revalidate", updated_at=datetime.now(UTC))
        )
        return _row(conn, source_runtime, source_runtime.c.id, 1)
