"""Read immutable states with snapshot isolation and scoped keyset cursors."""

import base64
import json
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, text

from .codec import decode_json
from .errors import StorageError
from .mapping import date_text, decoded_optional, record_from_row
from .records import comment_select
from .schema import comments, discussion_states, videos


@contextmanager
def read_snapshot(conn):
    """Reuse an explicit repeatable snapshot; never commit a caller's transaction."""
    if conn.in_transaction():
        if conn.get_isolation_level() not in {"REPEATABLE READ", "SERIALIZABLE"}:
            raise StorageError("snapshot_required")
        yield
        return
    with conn.begin():
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        yield


def _state(conn, state_id, video_id=None):
    try:
        if str(UUID(state_id)) != state_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise StorageError("invalid_state_id") from exc
    query = select(discussion_states).where(discussion_states.c.state_id == state_id)
    if video_id is not None:
        query = query.where(discussion_states.c.video_id == video_id)
    row = conn.execute(query).mappings().first()
    if row is None:
        raise StorageError("state_expired")
    if row["lifecycle"] not in {"current", "ready", "partial"}:
        raise StorageError("state_not_readable")
    return row


def read_state(conn, video_id: str, state_id: str | None = None) -> dict:
    with read_snapshot(conn):
        if state_id is None:
            video = (
                conn.execute(select(videos).where(videos.c.video_id == video_id)).mappings().first()
            )
            if video is None:
                raise StorageError("not_published")
            state_id = video["current_state_id"] or video["working_state_id"]
            if state_id is None:
                raise StorageError("not_published")
        row = _state(conn, state_id, video_id)
        result = dict(row)
        for key in ("coverage", "source_metadata"):
            result[key] = decode_json(result[key])
        result["title"] = decoded_optional(result["title"])
        for key in ("captured_from", "captured_to", "exported_at", "hour_bucket"):
            result[key] = result[key].isoformat()
        return result


def _cursor_read(cursor, state_id, kind, identity):
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if value["state_id"] != state_id or value["query"] != kind or value["identity"] != identity:
            raise ValueError
        cid = value["comment_id"]
        if not isinstance(cid, str) or not cid.isascii() or not cid.isdigit() or cid[0] == "0":
            raise ValueError
        if type(value["id_length"]) is not int or value["id_length"] != len(cid):
            raise ValueError
        if type(value["null_time"]) is not bool:
            raise ValueError
        stamp = value["created_at"]
        if value["null_time"] != (stamp is None):
            raise ValueError
        if stamp is not None:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None or date_text(parsed) != stamp:
                raise ValueError
        if kind == "thread" and (
            type(value["root_rank"]) is not int or value["root_rank"] not in (0, 1)
        ):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise StorageError("invalid_cursor") from exc


def _after(value, kind):
    c = comments.c
    id_after = or_(
        c.id_length > value["id_length"],
        and_(c.id_length == value["id_length"], c.comment_id > value["comment_id"]),
    )
    if value["null_time"]:
        after_time = and_(c.created_at.is_(None), id_after)
    else:
        stamp = datetime.fromisoformat(value["created_at"])
        after_time = or_(
            c.created_at.is_(None), c.created_at > stamp, and_(c.created_at == stamp, id_after)
        )
    if kind == "thread":
        return or_(
            c.root_rank > value["root_rank"], and_(c.root_rank == value["root_rank"], after_time)
        )
    return after_time


def _page(conn, state_id, identity, kind, limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise StorageError("invalid_limit")
    if identity is not None and (
        not isinstance(identity, str)
        or not identity.isascii()
        or not identity.isdigit()
        or identity.startswith("0")
    ):
        raise StorageError("invalid_identity")
    if kind == "thread" and identity is None:
        raise StorageError("invalid_identity")
    value = _cursor_read(cursor, state_id, kind, identity) if cursor is not None else None
    with read_snapshot(conn):
        state = _state(conn, state_id)
        c = comments.c
        owner = c.root_id if kind == "thread" else c.author_uid
        query = comment_select().where(c.state_id == state_id, owner == identity)
        if value is not None:
            query = query.where(_after(value, kind))
        order = [c.root_rank] if kind == "thread" else []
        query = query.order_by(
            *order, c.created_at.asc().nulls_last(), c.id_length, c.comment_id
        ).limit(limit + 1)
        rows = conn.execute(query).mappings().all()
        items = [record_from_row(row, state) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            data = {
                "state_id": state_id,
                "query": kind,
                "identity": identity,
                "null_time": last["created_at"] is None,
                "created_at": date_text(last["created_at"]),
                "id_length": last["id_length"],
                "comment_id": last["comment_id"],
            }
            if kind == "thread":
                data["root_rank"] = last["root_rank"]
            next_cursor = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        return {"items": items, "next_cursor": next_cursor}


def list_thread_comments(
    conn, state_id: str, identity: str, limit: int = 100, cursor: str | None = None
) -> dict:
    return _page(conn, state_id, identity, "thread", limit, cursor)


def list_user_comments(
    conn, state_id: str, identity: str | None, limit: int = 100, cursor: str | None = None
) -> dict:
    return _page(conn, state_id, identity, "user", limit, cursor)
