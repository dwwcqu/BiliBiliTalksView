"""Copy a readable database state into a self-contained local refresh baseline."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

from app.comment_export.checkpoint import Checkpoint
from app.comment_export.publication import exclusive_lock

from .codec import decode_json
from .errors import StorageError
from .mapping import record_from_row
from .queries import read_snapshot
from .records import comment_select
from .schema import comments, discussion_states, threads, unclassified_comments, videos
from .tail_evidence import tail_for_state
from .zero_evidence import filter_zero_extensions


def freeze_baseline(conn, video_id: str, target: Path) -> dict:
    target = Path(target)
    with exclusive_lock(target / ".collect.lock"):
        if (target / "work.sqlite3").exists():
            raise StorageError("baseline_exists")
        with read_snapshot(conn):
            video = (
                conn.execute(select(videos).where(videos.c.video_id == video_id))
                .mappings()
                .one_or_none()
            )
            result = {
                "video_id": video_id,
                "state_id": None,
                "cache_version": video["cache_version"] if video else 0,
            }
            if video is None:
                return result
            candidates = conn.execute(
                select(discussion_states)
                .where(
                    discussion_states.c.video_id == video_id,
                    discussion_states.c.state_id.in_(
                        [
                            video["current_state_id"],
                            video["working_state_id"],
                        ]
                    ),
                    discussion_states.c.lifecycle.in_(["current", "partial"]),
                )
                .order_by(
                    discussion_states.c.captured_to.desc(),
                    discussion_states.c.exported_at.desc(),
                    (discussion_states.c.state_id == video["working_state_id"]).desc(),
                )
            ).mappings()
            state = candidates.first()
            if state is None:
                return result
            sid = str(state["state_id"])
            result["state_id"] = sid
            metadata = deepcopy(decode_json(state["source_metadata"])["manifest"])
            for key in ("_refresh", "_threads", "_unclassified"):
                metadata.pop(key, None)
            metadata["_threads"] = {}
            for row in conn.execute(select(threads).where(threads.c.state_id == sid)).mappings():
                coverage = decode_json(row["coverage"])
                metadata["_threads"][row["root_id"]] = {
                    "pagination_status": coverage["pagination_status"],
                    "count": coverage["source_reported_reply_count"],
                }
            if state["refresh_context"] is not None:
                context = decode_json(state["refresh_context"])
                if context["video_id"] != video_id or context["version"] != 1:
                    raise StorageError("invalid_stored_handoff")
                metadata["_refresh"] = deepcopy(context["evidence"]["refresh"])
                for root, evidence in context["evidence"]["threads"].items():
                    if root not in metadata["_threads"]:
                        raise StorageError("invalid_stored_handoff")
                    metadata["_threads"][root].update(deepcopy(evidence))
            rows = [
                record_from_row(row, state)
                for row in conn.execute(
                    comment_select().where(comments.c.state_id == sid)
                ).mappings()
            ]
            by_id = {row['comment_id']: row for row in rows}
            for root, evidence in metadata['_threads'].items():
                tail = tail_for_state(evidence, by_id.get(root), by_id, metadata['captured_to'])
                evidence.pop('tail_evidence', None)
                if tail is not None:
                    evidence['tail_evidence'] = tail
            zero_refresh, zero_threads, _ = filter_zero_extensions(metadata, rows)
            if '_refresh' in metadata:
                for key in ('zero_policy_snapshot', 'zero_reply_schedule'):
                    metadata['_refresh'].pop(key, None)
                metadata['_refresh'].update(zero_refresh)
            for root, fields in zero_threads.items():
                for key in ('zero_reply_history', 'zero_reply_evidence'):
                    metadata['_threads'][root].pop(key, None)
                metadata['_threads'][root].update(fields)
            metadata["_unclassified"] = [
                decode_json(row)
                for row in conn.execute(
                    select(unclassified_comments.c.payload)
                    .where(unclassified_comments.c.state_id == sid)
                    .order_by(unclassified_comments.c.ordinal)
                ).scalars()
            ]
        # The PostgreSQL snapshot is released before filesystem writes. Its version is
        # later checked by materialize; state reclamation cannot invalidate this copy.
        with TemporaryDirectory(prefix=".baseline-", dir=target) as temporary:
            staged = Path(temporary) / "work.sqlite3"
            checkpoint = Checkpoint(staged)
            try:
                checkpoint.commit_page("database:baseline", rows, {
                    "metadata": metadata, "finished": True, "blocked": False,
                    "database_baseline": result,
                })
            finally:
                checkpoint.close()
            destination = target / "work.sqlite3"
            if destination.exists():
                raise StorageError("baseline_exists")
            staged.rename(destination)
        return result
