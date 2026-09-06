"""Freeze a database snapshot and reuse the fixed file export protocol."""

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import select

from app.comment_export.export import LATEST_SCHEMA_VERSION, build_batch, write_json
from app.comment_export.publication import publish_batch
from app.comment_export.validation import read_json, validate_batch

from .codec import decode_json
from .mapping import record_from_row
from .queries import read_snapshot, read_state
from .records import comment_select
from .schema import comments, threads, unclassified_comments


def export_state(conn, video_id: str, output: Path, state_id: str | None = None) -> Path:
    with read_snapshot(conn):
        state = read_state(conn, video_id, state_id)
        source = state["source_metadata"]
        manifest = deepcopy(source["manifest"])
        source_users = {info["uid"]: info for info in source.get("users", [])}
        sid = state["state_id"]
        source_threads = {}
        for row in conn.execute(select(threads).where(threads.c.state_id == sid)).mappings():
            source_threads[row["root_id"]] = decode_json(row["source_metadata"])
        records = [
            record_from_row(row, state)
            for row in conn.execute(comment_select().where(comments.c.state_id == sid)).mappings()
        ]
        unclassified = [
            decode_json(row["payload"])
            for row in conn.execute(
                select(unclassified_comments)
                .where(unclassified_comments.c.state_id == sid)
                .order_by(unclassified_comments.c.ordinal)
            ).mappings()
        ]
    # All inputs are now local: filesystem work never prolongs the database snapshot.
    export_id = str(uuid4())
    manifest["export_id"] = export_id
    manifest["schema_version"] = LATEST_SCHEMA_VERSION
    manifest["exported_at"] = max(
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), manifest["captured_to"]
    )
    manifest["_threads"] = {
        root: {
            "pagination_status": info["coverage"]["pagination_status"],
            "count": info["coverage"]["source_reported_reply_count"],
            "reasons": info["coverage"]["reasons"],
        }
        for root, info in source_threads.items()
    }
    manifest["_unclassified"] = unclassified
    for record in records:
        record["export_id"] = export_id
        record["schema_version"] = LATEST_SCHEMA_VERSION
    with TemporaryDirectory(prefix="bilibili-db-export-") as work:
        batch = build_batch(records, manifest, Path(work) / "batch")
        generated = read_json(batch / "manifest.json", "manifest")
        # The builder derives directories and projections. Preserve source metadata
        # (including extensions and source titles), changing only batch identity/paths.
        generated["coverage"] = deepcopy(manifest["coverage"])
        for entry in generated["threads"]:
            path = batch / entry["path"]
            derived = read_json(path, "thread")
            original = deepcopy(source_threads[entry["root_id"]])
            original.update(export_id=export_id, schema_version=LATEST_SCHEMA_VERSION, comments_path=derived["comments_path"])
            write_json(path, original)
        for entry in generated["users"]:
            path = batch / entry["path"]
            derived = read_json(path, "user")
            info = {**deepcopy(source_users.get(entry["uid"], {})), **derived}
            info.update(
                coverage_status=generated["coverage"]["status"],
                context_status=generated["coverage"]["context_status"],
                reasons=deepcopy(generated["coverage"]["reasons"]),
            )
            write_json(path, info)
        write_json(batch / "manifest.json", generated)
        validate_batch(batch)
        return publish_batch(batch, Path(output) / ("bilibili-video-" + manifest["source"]["aid"]))
