"""Read-only diagnosis and bounded, explicitly requested access recovery."""

import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .access_control import AccessControl, AccessControlError
from .availability import commit_unavailable
from .checkpoint import Checkpoint, read_control_snapshot
from .contract import ContractError
from .diagnostics import FailureDetail, is_reply_unavailable, utc_now
from .incremental import thread_done
from .normalization import normalize_comment
from .publication import exclusive_lock
from .recovery_gate import mint_proof
from .request_budget import task_budget
from .source import CollectionStopped, fetch_main, fetch_replies, get_signing_keys, resolve_video

_RECOVERABLE = {
    "access_restricted",
    "authentication_required",
    "rate_limited",
    "request_rejected",
    "source_unavailable",
    "network_error",
    "unknown_legacy",
    "resource_unavailable",
}


def _failure(progress):
    detail = progress.get("failure")
    try:
        return FailureDetail(**detail) if isinstance(detail, dict) else None
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid_control_state") from exc


def select_target(snapshot, next_pending=False):
    progress, metadata = snapshot["progress"], snapshot["metadata"]
    failure = _failure(progress)
    if failure and failure.category not in _RECOVERABLE:
        raise CollectionStopped("local_validation_blocked")
    if failure and failure.category != "unknown_legacy":
        if failure.checkpoint_revision != progress.get("checkpoint_revision", 0):
            raise CollectionStopped("failure_target_stale")
        chosen = {"phase": failure.phase, **failure.target}
        if failure.phase in {"main", "replies"}:
            expected = _next_target(progress, metadata)
            if chosen != expected:
                raise CollectionStopped("failure_target_stale")
        if failure.phase == "signing_keys" and progress.get("main_done"):
            raise CollectionStopped("failure_target_stale")
        return chosen
    if not next_pending:
        raise CollectionStopped("legacy_target_requires_next_pending")
    if failure is None and progress.get("stopped_reason") not in {
        "access_restricted",
        "http_error",
        "network_error",
    }:
        raise CollectionStopped("local_validation_blocked")
    return _next_target(progress, metadata)


def _next_target(progress, metadata):
    if not metadata.get("source"):
        raise CollectionStopped("legacy_target_unavailable")
    if not progress.get("main_done"):
        cursor = progress.get("cursor", "")
        if not isinstance(cursor, str):
            raise CollectionStopped("legacy_target_unavailable")
        return {"phase": "main", "cursor": cursor}
    for root, state in metadata.get("_threads", {}).items():
        if thread_done(state):
            continue
        page = state.get("page", 1)
        if (
            not str(root).isdigit()
            or str(root).startswith("0")
            or type(page) is not int
            or not 1 <= page <= 1000
        ):
            raise CollectionStopped("legacy_target_unavailable")
        return {"phase": "replies", "root_id": root, "page": page}
    raise CollectionStopped("legacy_target_unavailable")


def diagnose(work_dir: Path, control_dir: Path) -> dict:
    snapshot = read_control_snapshot(work_dir)
    progress = snapshot["progress"]
    failure = _failure(progress)
    status = AccessControl(control_dir).read_status()
    legacy = failure is None and progress.get("blocked", False)
    public = failure.to_public_dict() if failure else None
    if legacy:
        public = {
            "failure_id": None,
            "category": "unknown_legacy",
            "http_status": None,
            "api_code": None,
            "safe_reason": "legacy_failure_details_missing",
        }
    restrictions = []
    if not progress.get("blocked", False):
        restrictions.append("task_not_blocked")
    candidate_target = None
    try:
        selected = select_target(snapshot, next_pending=True)
        candidate_target = FailureDetail(phase=selected["phase"],
            target={k: v for k, v in selected.items() if k != "phase"}).to_public_dict()["target"]
        candidate_target["phase"] = selected["phase"]
    except CollectionStopped as exc:
        restrictions.append(exc.reason)
    if status["cooldown_until"] is not None and time.time() < status["cooldown_until"]:
        restrictions.append("cooldown_active")
    if legacy and not progress.get("legacy_registered_at"):
        restrictions.append("legacy_registration_required")
    remaining = max(0, progress.get("max_requests", 12000) - progress.get("requests", 0))
    required_requests = 2 if candidate_target and candidate_target["phase"] in {"main", "resolve"} else 1
    if remaining < required_requests:
        restrictions.append("budget_exhausted")
    public_control = {
        key: datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if value is not None
        else None
        for key, value in status.items()
    }
    return {
        "blocked": progress.get("blocked", False),
        "failure": public,
        "can_probe": not restrictions,
        "next_pending_target": candidate_target,
        "restrictions": restrictions,
        "checkpoint_revision": progress.get("checkpoint_revision", 0),
        "requests": progress.get("requests", 0),
        "remaining_requests": max(
            0, progress.get("max_requests", 12000) - progress.get("requests", 0)
        ),
        "comment_count": snapshot["comment_count"],
        "control": public_control,
        "legacy_target_selection_required": legacy
        or bool(failure and failure.category == "unknown_legacy"),
    }


def _save_failure(cp, client, exc):
    progress = cp.get_progress()
    detail = getattr(exc, "detail", None)
    if detail is None:
        prior = getattr(client, "last_diagnostic", None)
        detail = (
            replace(prior, category="invalid_response", safe_reason="invalid_response")
            if prior
            else FailureDetail()
        )
    detail = replace(detail, checkpoint_revision=progress.get("checkpoint_revision", 0))
    progress.update(blocked=True, stopped_reason=detail.safe_reason, failure=detail.to_dict())
    cp.set_progress(progress)
    return detail


def _probe_locked(work_dir, client, next_pending):
    if not hasattr(client, "access_policy"):
        raise CollectionStopped("shared_control_required")
    snapshot = read_control_snapshot(work_dir)
    progress, metadata = snapshot["progress"], snapshot["metadata"]
    if not progress.get("blocked"):
        raise CollectionStopped("task_not_blocked")
    target = select_target(snapshot, next_pending)
    cp = Checkpoint(Path(work_dir) / "work.sqlite3")
    try:
        failure = _failure(progress)
        if failure is None or failure.category == "unknown_legacy":
            if not progress.get("legacy_registered_at"):
                deadline = client.access_policy.clock() + 1800
                detail = FailureDetail(
                    category="unknown_legacy",
                    phase=target["phase"],
                    endpoint="/x/v2/reply/reply"
                    if target["phase"] == "replies"
                    else "/x/v2/reply/wbi/main",
                    video_id=metadata.get("video_id"),
                    target={k: v for k, v in target.items() if k != "phase"},
                    checkpoint_revision=progress.get("checkpoint_revision", 0),
                    safe_reason="legacy_registered",
                )
                progress.update(
                    failure=detail.to_dict(),
                    legacy_registered_at=utc_now(),
                    legacy_cooldown_until=deadline,
                )
                cp.set_progress(progress)
            deadline = progress.get("legacy_cooldown_until")
            if deadline is None:
                deadline = (
                    datetime.fromisoformat(progress["legacy_registered_at"]).timestamp() + 1800
                )
            client.access_policy.register_legacy_cooldown(until=deadline)
            if client.access_policy.clock() < deadline:
                raise AccessControlError("cooldown_active")
        limit = 1 if target["phase"] in {"replies", "signing_keys"} else 2
        with task_budget(cp, client, progress.get("max_requests", 12000), call_limit=limit):
            if target["phase"] == "replies":
                data = fetch_replies(metadata["source"], target["root_id"], target["page"], client)
                page = data["page"]
                rows = data["replies"] or []
                state = metadata["_threads"][target["root_id"]]
                seen = set(state.get("seen", []))
                returned = set()
                for raw in [data["root"], *rows]:
                    record = normalize_comment(
                        raw,
                        target["root_id"],
                        metadata["video_id"],
                        metadata["export_id"],
                        utc_now(),
                    )
                    if record["kind"] == "reply":
                        if record["comment_id"] in returned:
                            raise CollectionStopped("invalid_probe_page")
                        returned.add(record["comment_id"])
                tail = page["num"] * page["size"] >= page["count"]
                if (
                    len(rows) > page["size"]
                    or (returned and not returned - seen)
                    or len(seen | returned) > page["count"]
                    or (not rows and not tail)
                    or (tail and len(seen | returned) != page["count"])
                ):
                    raise CollectionStopped("invalid_probe_page")
            elif target["phase"] == "main":
                keys = get_signing_keys(client)
                data = fetch_main(metadata["source"], target["cursor"], client, keys)
                if not data["replies"] and not data["cursor"]["is_end"]:
                    raise CollectionStopped("invalid_probe_page")
                for raw in data["replies"] or []:
                    row = normalize_comment(
                        raw, None, metadata["video_id"], metadata["export_id"], utc_now()
                    )
                    if row["kind"] != "root":
                        raise CollectionStopped("invalid_probe_page")
            elif target["phase"] == "signing_keys":
                get_signing_keys(client)
            elif target["phase"] == "resolve":
                url = metadata.get("input_url") or progress.get("input_url")
                if not url:
                    raise CollectionStopped("legacy_target_unavailable")
                result = resolve_video(url, client)
                if metadata.get("source") and result["source"]["oid"] != metadata["source"]["oid"]:
                    raise CollectionStopped("video_identity_mismatch")
                for name in ("aid", "bvid", "episode_id"):
                    if name in target and result["source"].get(name) != target[name]:
                        raise CollectionStopped("video_identity_mismatch")
            else:
                raise CollectionStopped("unsupported_probe_phase")
        progress = cp.get_progress()
        # One durable failure remains the proof's identity; probe never resolves it.
        failure = _failure(progress)
        progress["probe_summary"] = {
            "checked_at": utc_now(),
            "failure_id": failure.failure_id,
            "checkpoint_revision": progress.get("checkpoint_revision", 0),
            "ok": True,
        }
        cp.set_progress(progress)
        return {
            "probe_ok": True,
            "blocked": True,
            "target": failure.to_public_dict()["target"],
            "failure_id": failure.failure_id,
        }, mint_proof(work_dir, client, progress, target)
    except (CollectionStopped, ContractError) as exc:
        if getattr(exc, "reason", None) in {
            "budget_exhausted",
            "probe_request_limit",
            "legacy_target_unavailable",
        }:
            raise
        detail = _save_failure(cp, client, exc)
        raise CollectionStopped(detail.safe_reason, detail) from None
    finally:
        cp.close()


def probe(work_dir: Path, client, next_pending=False) -> dict:
    read_control_snapshot(work_dir)
    with exclusive_lock(Path(work_dir) / ".collect.lock"):
        return _probe_locked(work_dir, client, next_pending)[0]


def recover(work_dir: Path, client, output: Path, next_pending=False) -> dict:
    from .cli import export_work
    from .collector import collect

    read_control_snapshot(work_dir)
    with exclusive_lock(Path(work_dir) / ".collect.lock"):
        proof = None
        try:
            _, proof = _probe_locked(work_dir, client, next_pending)
        except CollectionStopped as exc:
            if not is_reply_unavailable(exc.detail):
                raise
            cp = Checkpoint(Path(work_dir) / "work.sqlite3")
            try:
                commit_unavailable(cp, exc.detail)
            finally:
                cp.close()
        snapshot = read_control_snapshot(work_dir)
        metadata, progress = snapshot["metadata"], snapshot["progress"]
        url = metadata.get("input_url") or progress.get("input_url")
        _rows, result = collect(
            url,
            Path(work_dir),
            client,
            progress.get("max_requests", 12000),
            resume=True,
            recovery_proof=proof,
        )
        final = read_control_snapshot(work_dir)
        response = {
            "collection": {
                "finished": final["progress"].get("finished", False),
                "blocked": final["progress"].get("blocked", False),
                "coverage": deepcopy(result["coverage"]),
            }
        }
        try:
            path, manifest = export_work(Path(work_dir), Path(output))
            response["export"] = {"path": str(path), "coverage": manifest["coverage"]}
        except (OSError, ValueError):
            response["export"] = {"error": "export_failed"}
        response["diagnostic"] = diagnose(work_dir, client.access_policy.directory)
        return response
