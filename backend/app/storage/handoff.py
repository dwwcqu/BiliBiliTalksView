"""Bind collector evidence to a batch and materialize it with an atomic commit guard."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update

from app.comment_export.incremental import root_signature
from app.comment_export.validation import read_json, read_lines, safe_path

from .codec import decode_json, encode_json
from .errors import StorageError
from .importer import _import_owned
from .locks import video_lock
from .schema import discussion_states as states
from .schema import import_receipts as receipts
from .schema import videos
from .tail_evidence import tail_for_state
from .zero_evidence import check_zero_history_transition, filter_zero_extensions


@dataclass(frozen=True)
class Handoff:
    job_id: str
    video_id: str
    baseline_version: int
    batch_digest: str
    context_json: str


def _record(row):
    return {key: value for key, value in row.items() if key not in {"schema_version", "export_id"}}


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp_requires_zone")
    return parsed


def prepare_handoff(frozen, records, metadata, job_id: str, baseline_version: int) -> Handoff:
    try:
        if str(UUID(job_id)) != job_id or type(baseline_version) is not int or baseline_version < 0:
            raise ValueError
        manifest = frozen.manifest
        if metadata["video_id"] != manifest["video_id"] or metadata["source"] != manifest["source"]:
            raise ValueError
        for field in ("captured_from", "captured_to", "hour_bucket"):
            if _stamp(metadata[field]) != _stamp(manifest[field]):
                raise ValueError
        original = {row["comment_id"]: _record(row) for row in records}
        if len(original) != len(records):
            raise ValueError
        actual, thread_files = {}, {}
        for entry in manifest["threads"]:
            info = read_json(safe_path(frozen.directory, entry["path"]), "thread")
            thread_files[info["root_id"]] = info
            for row in read_lines(safe_path(frozen.directory, info["comments_path"])):
                actual[row["comment_id"]] = _record(row)
        if original != actual or set(thread_files) != set(metadata["_threads"]):
            raise ValueError
        exceptions = (
            read_lines(safe_path(frozen.directory, manifest["unclassified_path"]), "unclassified")
            if manifest["unclassified_path"]
            else []
        )
        if exceptions != metadata.get("_unclassified", []):
            raise ValueError
        refresh = metadata["_refresh"]
        if refresh["version"] != 1 or refresh["mode"] not in {"full", "incremental"}:
            raise ValueError
        if type(refresh["full_scan_incomplete"]) is not bool:
            raise ValueError
        observed_list = refresh["observed_ids"]
        observed = set(observed_list)
        if len(observed) != len(observed_list) or not observed <= set(original):
            raise ValueError
        main_ids = set(refresh["observed_main_ids"])
        if not main_ids <= {cid for cid in observed if original[cid]["kind"] == "root"}:
            raise ValueError
        last_full = refresh.get("last_full_scan_completed_at")
        if last_full is not None and _stamp(last_full) > _stamp(metadata["captured_to"]):
            raise ValueError
        compact_threads = {}
        for root, state in metadata["_threads"].items():
            info = thread_files[root]
            if (
                state["pagination_status"] != info["coverage"]["pagination_status"]
                or state.get("count") != info["coverage"]["source_reported_reply_count"]
            ):
                raise ValueError
            check = state.get("reply_check_state", "needs_check")
            verification = state.get("reply_verification", "not_checked")
            if check not in {
                "complete",
                "needs_check",
                "source_unavailable",
            } or verification not in {
                "checked_now",
                "not_checked",
                "reused_unverified",
                "source_unavailable",
            }:
                raise ValueError
            if check == "complete":
                if (
                    type(state.get("checked_count")) is not int
                    or state["checked_count"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", state.get("checked_root", ""))
                    or _stamp(state["last_complete_at"]) > _stamp(metadata["captured_to"])
                ):
                    raise ValueError
                if verification == "checked_now":
                    checked_ids = {
                        cid
                        for cid in observed
                        if original[cid]["root_id"] == root and original[cid]["kind"] == "reply"
                    }
                    if (
                        len(checked_ids) != state["checked_count"]
                        or root not in observed
                        or root_signature(original[root]) != state["checked_root"]
                    ):
                        raise ValueError
            inherited = any(
                cid not in observed and row["root_id"] == root and row["kind"] == "reply"
                for cid, row in original.items()
            )
            if (inherited or verification == "reused_unverified") and state[
                "pagination_status"
            ] == "verified":
                raise ValueError
            compact_threads[root] = {
                key: state[key]
                for key in (
                    "pagination_status",
                    "count",
                    "last_complete_at",
                    "last_reply_checked_at",
                    "checked_root",
                    "checked_count",
                    "unavailable",
                )
                if key in state
            }
            compact_threads[root].update(reply_check_state=check, reply_verification=verification)
            tail = tail_for_state(state, original.get(root), original, metadata['captured_to'])
            if tail is not None:
                compact_threads[root]['tail_evidence'] = tail
        if (
            any(row["kind"] == "root" and cid not in observed for cid, row in original.items())
            and manifest["coverage"]["main_pagination"] == "verified"
        ):
            raise ValueError
        if not refresh["full_scan_incomplete"]:
            if last_full is None:
                raise ValueError
            if refresh["mode"] == "full" and any(
                state["reply_check_state"] == "needs_check" for state in compact_threads.values()
            ):
                raise ValueError
        evidence = {
            "refresh": {
                key: refresh[key]
                for key in (
                    "version",
                    "mode",
                    "requested_mode",
                    "started_at",
                    "full_scan_incomplete",
                    "last_full_scan_completed_at",
                    "observed_ids",
                    "observed_main_ids",
                )
                if key in refresh
            },
            "threads": compact_threads,
        }
        zero_refresh, zero_threads, zero_summary = filter_zero_extensions(
            metadata, list(original.values()), strict=True, work_id=job_id)
        evidence['refresh'].update(zero_refresh)
        for root, fields in zero_threads.items():
            evidence['threads'][root].update(fields)
        envelope = {
            "version": 1,
            "job_id": job_id,
            "video_id": manifest["video_id"],
            "baseline_version": baseline_version,
            "batch_digest": frozen.digest,
            "evidence": evidence,
        }
        if zero_summary is not None:
            envelope['zero_reply_summary'] = zero_summary
        return Handoff(
            job_id,
            manifest["video_id"],
            baseline_version,
            frozen.digest,
            json.dumps(envelope, ensure_ascii=True, sort_keys=True, allow_nan=False),
        )
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise StorageError("invalid_collection_handoff") from exc


def materialize(conn, frozen, handoff: Handoff, commit_guard) -> dict:
    """Caller supplies a guard which fences and updates its job in this same transaction."""
    if not callable(commit_guard):
        raise StorageError("commit_guard_required")
    try:
        envelope = json.loads(handoff.context_json)
        if (
            handoff.video_id != frozen.manifest["video_id"]
            or handoff.batch_digest != frozen.digest
            or any(
                envelope[key] != getattr(handoff, key)
                for key in ("video_id", "job_id", "baseline_version", "batch_digest")
            )
        ):
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise StorageError("handoff_batch_mismatch") from exc
    with video_lock(conn, handoff.video_id):
        with conn.begin():
            video = (
                conn.execute(
                    select(videos).where(videos.c.video_id == handoff.video_id).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            version = video["cache_version"] if video is not None else 0
            prior = conn.execute(
                select(states.c.refresh_context)
                .join(receipts, receipts.c.state_id == states.c.state_id)
                .where(
                    receipts.c.video_id == handoff.video_id,
                    receipts.c.source_export_id == frozen.manifest["export_id"],
                    receipts.c.canonical_digest == frozen.digest,
                    receipts.c.status.in_(["ready", "partial", "published"]),
                )
            ).scalar_one_or_none()
            if prior is not None and decode_json(prior) != envelope:
                raise StorageError("handoff_context_conflict")
            if prior is None and version != handoff.baseline_version:
                raise StorageError("baseline_changed")
            if video is not None:
                check_zero_history_transition(conn,
                    [video['current_state_id'], video['working_state_id']], envelope)
            result = _import_owned(conn, frozen, publish=True, atomic=True)
            existing = conn.execute(
                select(states.c.refresh_context).where(states.c.state_id == result["state_id"])
            ).scalar_one()
            if existing is not None and decode_json(existing) != envelope:
                raise StorageError("handoff_context_conflict")
            conn.execute(
                update(states)
                .where(states.c.state_id == result["state_id"])
                .values(refresh_context=encode_json(envelope))
            )
            if commit_guard(conn, result) is False:
                raise StorageError("commit_guard_rejected")
        return result
