"""Idempotent protocol import with per-video ownership and isolated working states."""

from contextlib import nullcontext
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.comment_export.dataset import ValidatedDataset

from .codec import decode_json, encode_json
from .errors import StorageError
from .frozen import FrozenBatch
from .locks import video_lock
from .mapping import date_value, encoded_optional, member_values, record_from_row
from .payloads import prune_payloads, resolve_payloads
from .publication import check_freshness, discard_state, publish_locked
from .records import comment_select
from .schema import comments, threads, unclassified_comments, video_links, videos
from .schema import discussion_states as states
from .schema import import_receipts as receipts
from .semantic import from_frozen, from_state


def _candidate(manifest: dict, state_id: str, user_metadata: list) -> dict:
    return {
        "state_id": state_id,
        "video_id": manifest["video_id"],
        "source_export_id": manifest["export_id"],
        "schema_version": manifest["schema_version"],
        **{
            key: date_value(manifest[key])
            for key in ("hour_bucket", "captured_from", "captured_to", "exported_at")
        },
        "coverage": encode_json(manifest["coverage"]),
        "title": encoded_optional(manifest["title"]),
        "source_metadata": encode_json({"manifest": manifest, "users": user_metadata}),
        "lifecycle": "loading",
    }


def _load(conn, frozen: FrozenBatch | ValidatedDataset, state_id: str, *, atomic: bool = False) -> None:
    manifest = frozen.manifest
    expected = {}
    for entry in manifest["threads"]:
        info = frozen.read_document(entry["path"])
        values = {
            "state_id": state_id,
            "root_id": info["root_id"],
            "root_author_uid": info["root_author"]["uid"],
            "root_author_name": encoded_optional(info["root_author"]["nickname"]),
            "source_title": encoded_optional(info["source_title"]),
            **{
                k: info[k]
                for k in (
                    "comment_count",
                    "reply_count",
                    "participant_count",
                    "unknown_author_comment_count",
                )
            },
            "coverage": encode_json(info["coverage"]),
            "source_metadata": encode_json(info),
        }
        rows = frozen.read_lines(info["comments_path"])
        with (nullcontext() if atomic else conn.begin()):
            conn.execute(insert(threads).values(**values))
        for start in range(0, len(rows), 1000):
            part = rows[start : start + 1000]
            with (nullcontext() if atomic else conn.begin()):
                payload_ids = resolve_payloads(conn, manifest["video_id"], part)
                conn.execute(insert(comments), [
                    member_values(r, state_id, payload_ids[r["comment_id"]]) for r in part
                ])
            expected.update({r["comment_id"]: r for r in part})
    exceptions = []
    if manifest["unclassified_path"]:
        exceptions = frozen.read_lines(manifest["unclassified_path"])
        with (nullcontext() if atomic else conn.begin()):
            conn.execute(
                insert(unclassified_comments),
                [
                    {"state_id": state_id, "ordinal": i, "payload": encode_json(row)}
                    for i, row in enumerate(exceptions)
                ],
            )
    with (nullcontext() if atomic else conn.begin()):
        state = conn.execute(select(states).where(states.c.state_id == state_id)).mappings().one()
        actual = {
            r["comment_id"]: record_from_row(r, state)
            for r in conn.execute(
                comment_select().where(comments.c.state_id == state_id)
            ).mappings()
        }
        if actual != expected or len(actual) != manifest["counts"]["comments"]:
            raise StorageError("import_verification_failed")
        actual_exceptions = [
            decode_json(value)
            for value in conn.execute(
                select(unclassified_comments.c.payload)
                .where(unclassified_comments.c.state_id == state_id)
                .order_by(unclassified_comments.c.ordinal)
            ).scalars()
        ]
        if actual_exceptions != exceptions:
            raise StorageError("import_verification_failed")


def _import_owned(conn, frozen: FrozenBatch | ValidatedDataset, publish: bool = False, *, atomic=False) -> dict:
    if atomic and not conn.in_transaction():
        raise StorageError("atomic_transaction_required")
    manifest = frozen.manifest
    video_id, export_id = manifest["video_id"], manifest["export_id"]
    receipt_key = (receipts.c.video_id == video_id) & (receipts.c.source_export_id == export_id)
    state_id = None
    with (nullcontext() if atomic else conn.begin()):
        source = manifest["source"]
        conn.execute(
            pg_insert(videos)
            .values(
                video_id=video_id,
                **{
                    key: source[key]
                    for key in ("platform", "aid", "oid", "comment_type", "bvid", "episode_id")
                },
            )
            .on_conflict_do_nothing(index_elements=[videos.c.video_id])
        )
        video = (
            conn.execute(select(videos).where(videos.c.video_id == video_id).with_for_update())
            .mappings()
            .one()
        )
        receipt = conn.execute(select(receipts).where(receipt_key)).mappings().one_or_none()
        if receipt and receipt["canonical_digest"] != frozen.digest:
            raise StorageError("export_id_conflict")
        if receipt:
            linked = (
                conn.execute(select(states).where(states.c.state_id == receipt["state_id"]))
                .mappings()
                .one_or_none()
                if receipt["state_id"]
                else None
            )
            status = receipt["status"]
            if status == "expired":
                valid = receipt["state_id"] is None
            elif status == "failed" and receipt["state_id"] is None:
                valid = True
            else:
                expected_kind = "current" if status == "published" else status
                expected_pointer = (
                    "current_state_id" if status == "published" else "working_state_id"
                )
                valid = (
                    linked is not None
                    and linked["lifecycle"] == expected_kind
                    and linked["source_export_id"] == export_id
                    and linked["video_id"] == video_id
                    and video[expected_pointer] == receipt["state_id"]
                )
            if not valid:
                raise StorageError("inconsistent_import_state")
        if receipt and receipt["status"] == "expired":
            raise StorageError("already_imported_expired")
        if receipt and receipt["status"] in {"ready", "partial", "published"}:
            state = (
                conn.execute(select(states).where(states.c.state_id == receipt["state_id"]))
                .mappings()
                .one_or_none()
            )
            expected_lifecycle = (
                "current" if receipt["status"] == "published" else receipt["status"]
            )
            if not state or state["lifecycle"] != expected_lifecycle:
                raise StorageError("inconsistent_import_state")
            state_id = state["state_id"]
            result = {
                "state_id": state_id,
                "receipt_status": receipt["status"],
                "published": receipt["status"] == "published",
                "coverage": decode_json(state["coverage"]),
            }
    if state_id:
        return publish_locked(conn, video_id, state_id, atomic=atomic) if publish else result
    state_id = str(uuid4())
    user_metadata = [
        frozen.read_document(entry["path"])
        for entry in manifest["users"]
    ]
    candidate = _candidate(manifest, state_id, user_metadata)
    with (nullcontext() if atomic else conn.begin()):
        video = (
            conn.execute(select(videos).where(videos.c.video_id == video_id).with_for_update())
            .mappings()
            .one()
        )
        for pointer in ("current_state_id", "working_state_id"):
            if video[pointer]:
                previous = (
                    conn.execute(select(states).where(states.c.state_id == video[pointer]))
                    .mappings()
                    .one()
                )
                if previous["source_export_id"] != export_id:
                    check_freshness(
                        candidate,
                        previous,
                        equivalent=from_frozen(frozen) == from_state(conn, previous),
                    )
                if pointer == "working_state_id":
                    discard_state(conn, previous)
        conn.execute(insert(states).values(**candidate))
        conn.execute(
            update(videos)
            .where(videos.c.video_id == video_id)
            .values(working_state_id=state_id)
        )
        conn.execute(
            pg_insert(receipts)
            .values(
                video_id=video_id,
                source_export_id=export_id,
                canonical_digest=frozen.digest,
                state_id=state_id,
                status="loading",
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_update(
                index_elements=[receipts.c.video_id, receipts.c.source_export_id],
                set_={
                    "state_id": state_id,
                    "status": "loading",
                    "safe_error_code": None,
                    "completed_at": None,
                },
            )
        )
        for url in {manifest["input_url"], manifest["canonical_url"]}:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "www.bilibili.com":
                raise StorageError("invalid_source_url")
            normalized = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
            )
            conn.execute(
                pg_insert(video_links)
                .values(normalized_url=normalized, video_id=video_id)
                .on_conflict_do_nothing(index_elements=[video_links.c.normalized_url])
            )
            owner = conn.scalar(
                select(video_links.c.video_id).where(video_links.c.normalized_url == normalized)
            )
            if owner != video_id:
                raise StorageError("video_link_conflict")
    try:
        if atomic:
            _load(conn, frozen, state_id, atomic=True)
        else:
            _load(conn, frozen, state_id)
        lifecycle = "ready" if manifest["coverage"]["status"] == "verified" else "partial"
        with (nullcontext() if atomic else conn.begin()):
            prune_payloads(conn, video_id)
            conn.execute(
                update(states).where(states.c.state_id == state_id).values(lifecycle=lifecycle)
            )
            conn.execute(
                update(receipts)
                .where(receipt_key)
                .values(status=lifecycle, completed_at=datetime.now(UTC))
            )
    except (StorageError, SQLAlchemyError, ValueError, OSError) as exc:
        if atomic:
            code = exc.code if isinstance(exc, StorageError) else "database_error"
            raise StorageError(code) from None
        if conn.in_transaction():
            conn.rollback()
        code = exc.code if isinstance(exc, StorageError) else "database_error"
        if isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", "").startswith("22"):
            code = "unsupported_storage_value"
        if not conn.invalidated and not conn.closed:
            with (nullcontext() if atomic else conn.begin()):
                prune_payloads(conn, video_id)
                conn.execute(
                    update(states)
                    .where(states.c.state_id == state_id)
                    .values(lifecycle="failed")
                )
                conn.execute(
                    update(receipts)
                    .where(receipt_key)
                    .values(status="failed", safe_error_code=code)
                )
        raise StorageError(code) from None
    if publish:
        return publish_locked(conn, video_id, state_id, atomic=atomic)
    return {
        "state_id": state_id,
        "receipt_status": lifecycle,
        "published": False,
        "coverage": manifest["coverage"],
    }


def import_batch(conn, frozen: FrozenBatch | ValidatedDataset, publish: bool = False) -> dict:
    """Legacy chunk-committing import; atomic worker imports use handoff.materialize."""
    with video_lock(conn, frozen.manifest["video_id"]):
        return _import_owned(conn, frozen, publish)
