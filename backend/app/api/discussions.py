"""Public, state-scoped discussion reads; internal and unpublished states stay private."""

import base64
import json
import re
from datetime import datetime

from sqlalchemy import and_, func, or_, select

from app.jobs import repository
from app.jobs.schema import source_runtime
from app.storage.codec import decode_json
from app.storage.mapping import date_text, decoded_optional
from app.storage.queries import list_thread_comments, list_user_comments, read_snapshot
from app.storage.schema import comments, discussion_states, threads, videos
from app.storage.zero_evidence import public_zero_count

from .responses import APIError


def external_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise APIError("invalid_identity")
    return value


def video_identity(value):
    if not re.fullmatch(r"bilibili:video:[1-9][0-9]*", value):
        raise APIError("invalid_video_id")
    return value


def public_state(conn, state_id):
    row = (
        conn.execute(
            select(discussion_states)
            .join(videos, videos.c.video_id == discussion_states.c.video_id)
            .where(
                discussion_states.c.state_id == state_id,
                or_(
                    and_(
                        videos.c.current_state_id == state_id,
                        discussion_states.c.lifecycle == "current",
                    ),
                    and_(
                        videos.c.working_state_id == state_id,
                        discussion_states.c.lifecycle == "partial",
                    ),
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise APIError("state_expired", 410)
    return row


def summary(row):
    manifest = decode_json(row["source_metadata"])["manifest"]
    result = {
        "state_id": str(row["state_id"]),
        "title": decoded_optional(row["title"]),
        "coverage": decode_json(row["coverage"]),
        "counts": manifest["counts"],
        "hour_bucket": row["hour_bucket"],
        "captured_from": row["captured_from"],
        "captured_to": row["captured_to"],
        "published": row["lifecycle"] == "current",
    }
    try:
        context = decode_json(row.get('refresh_context'))
    except (ValueError, TypeError):
        context = None
    zero_count = public_zero_count(context, row['video_id'],
                                   result['counts'].get('root_comments'), result['coverage'],
                                   row['captured_to'])
    if zero_count is not None:
        result['zero_reply_observed_threads'] = zero_count
    return result


def video_view(conn, video_id):
    video_identity(video_id)
    with read_snapshot(conn):
        result = repository.video_view(conn, video_id)
        result["state"] = (
            summary(public_state(conn, result["state_id"])) if result["state_id"] else None
        )
        partial = result["partial_state_id"]
        result["partial_state"] = summary(public_state(conn, partial)) if partial else None
        runtime = (
            conn.execute(select(source_runtime).where(source_runtime.c.id == 1)).mappings().one()
        )
        result["source_access"] = {
            key: runtime[key] for key in ("source_gate", "safe_error", "blocked_at")
        }
        result.pop("cache_version", None)
        return result


def thread_page(conn, state_id, limit, cursor=None):
    value = None
    if cursor is not None:
        try:
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if value["state_id"] != state_id or value["kind"] != "threads":
                raise ValueError
            external_id(value["root_id"])
            if value["created_at"] is not None:
                stamp = datetime.fromisoformat(value["created_at"])
                if stamp.tzinfo is None or date_text(stamp) != value["created_at"]:
                    raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise APIError("invalid_cursor") from exc
    with read_snapshot(conn):
        public_state(conn, state_id)
        c, t = comments.c, threads.c
        query = (
            select(threads, c.created_at.label("root_created_at"))
            .select_from(
                threads.outerjoin(
                    comments,
                    and_(c.state_id == t.state_id, c.comment_id == t.root_id, c.kind == "root"),
                )
            )
            .where(t.state_id == state_id)
        )
        if value:
            last_id = value["root_id"]
            after_id = or_(
                func.length(t.root_id) > len(last_id),
                and_(func.length(t.root_id) == len(last_id), t.root_id > last_id),
            )
            if value["created_at"] is None:
                after = and_(c.created_at.is_(None), after_id)
            else:
                stamp = datetime.fromisoformat(value["created_at"])
                after = or_(
                    c.created_at.is_(None),
                    c.created_at > stamp,
                    and_(c.created_at == stamp, after_id),
                )
            query = query.where(after)
        rows = (
            conn.execute(
                query.order_by(
                    c.created_at.asc().nulls_last(), func.length(t.root_id), t.root_id
                ).limit(limit + 1)
            )
            .mappings()
            .all()
        )
        next_cursor = None
        if len(rows) > limit:
            row = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "state_id": state_id,
                        "kind": "threads",
                        "root_id": row["root_id"],
                        "created_at": date_text(row["root_created_at"]),
                    }
                ).encode()
            ).decode()
        items = [
            {
                "root_id": row["root_id"],
                "root_author": {
                    "uid": row["root_author_uid"],
                    "nickname": decoded_optional(row["root_author_name"]),
                },
                "source_title": decoded_optional(row["source_title"]),
                "comment_count": row["comment_count"],
                "reply_count": row["reply_count"],
                "participant_count": row["participant_count"],
                "unknown_author_comment_count": row["unknown_author_comment_count"],
                "coverage": decode_json(row["coverage"]),
            }
            for row in rows[:limit]
        ]
        return {"state_id": state_id, "items": items, "next_cursor": next_cursor}


def comment_page(conn, state_id, identity, kind, limit, cursor=None):
    if identity is not None:
        external_id(identity)
    with read_snapshot(conn):
        public_state(conn, state_id)
        if kind == "thread":
            if (
                conn.scalar(
                    select(threads.c.root_id).where(
                        threads.c.state_id == state_id, threads.c.root_id == identity
                    )
                )
                is None
            ):
                raise APIError("thread_not_found", 404)
            result = list_thread_comments(conn, state_id, identity, limit, cursor)
        else:
            result = list_user_comments(conn, state_id, identity, limit, cursor)
        return {"state_id": state_id, **result}
