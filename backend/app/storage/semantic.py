"""Compare observation content independently of export IDs and derived paths."""

from sqlalchemy import select

from .codec import decode_json
from .mapping import record_from_row
from .records import comment_select
from .schema import comments, threads, unclassified_comments


def _without(value, keys):
    # Versions 1 and 2 differ in directory layout, not observation semantics.
    if str(value.get("schema_version", "")).split(".")[0] in {"1", "2"}:
        keys = set(keys) | {"schema_version"}
    return {k: v for k, v in value.items() if k not in keys}


def observation(manifest, thread_records, user_records, records, exceptions):
    return {
        "manifest": _without(
            manifest, {"export_id", "exported_at", "threads", "users", "unclassified_path"}
        ),
        "threads": {
            r["root_id"]: _without(r, {"export_id", "comments_path"}) for r in thread_records
        },
        "users": {r["uid"]: _without(r, {"export_id", "comments_path"}) for r in user_records},
        "comments": {r["comment_id"]: _without(r, {"export_id"}) for r in records},
        "unclassified": exceptions,
    }


def from_frozen(frozen):
    manifest = frozen.manifest
    thread_records, records = [], []
    for entry in manifest["threads"]:
        info = frozen.read_document(entry["path"])
        thread_records.append(info)
        records.extend(frozen.read_lines(info["comments_path"]))
    users = [
        frozen.read_document(entry["path"]) for entry in manifest["users"]
    ]
    exceptions = (
        frozen.read_lines(manifest["unclassified_path"])
        if manifest["unclassified_path"]
        else []
    )
    return observation(manifest, thread_records, users, records, exceptions)


def from_state(conn, state):
    source = decode_json(state["source_metadata"])
    sid = state["state_id"]
    thread_records = [
        decode_json(r)
        for r in conn.execute(
            select(threads.c.source_metadata).where(threads.c.state_id == sid)
        ).scalars()
    ]
    records = [
        record_from_row(r, state)
        for r in conn.execute(comment_select().where(comments.c.state_id == sid)).mappings()
    ]
    exceptions = [
        decode_json(r)
        for r in conn.execute(
            select(unclassified_comments.c.payload)
            .where(unclassified_comments.c.state_id == sid)
            .order_by(unclassified_comments.c.ordinal)
        ).scalars()
    ]
    return observation(source["manifest"], thread_records, source["users"], records, exceptions)
