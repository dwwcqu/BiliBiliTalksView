"""Persistent refresh evidence, independent of exported coverage and scheduling."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .contract import ContractError
from .tail import select_tail_plan, validate_tail_evidence


def digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii")).hexdigest()


def root_signature(row: dict) -> str:
    return digest(row["content"])


def fingerprints(row: dict) -> dict:
    small = {key: value for key, value in row.items() if key not in {
        "schema_version", "export_id", "collected_at", "content",
    }}
    return {"body": digest(row["content"]), "metadata": digest(small)}


def start_tracking(metadata: dict, requested_mode: str = "full") -> None:
    if "_refresh" in metadata:
        return
    metadata["_refresh"] = {
        "version": 1, "requested_mode": requested_mode, "mode": "full",
        "started_at": metadata["captured_from"], "last_full_scan_completed_at": None,
        "full_scan_incomplete": True, "baseline_digest": None, "baseline_fingerprints": {},
        "observed_ids": [], "observed_main_ids": [],
    }
    for state in metadata.get("_threads", {}).values():
        state.setdefault("reply_check_state", "complete" if
                         state.get("pagination_status") == "verified" else "needs_check")


def prepare_refresh(records, metadata, progress, mode, observed, full_interval_hours):
    """Freeze a new observation from trusted work evidence or conservatively imported files."""
    stamp = datetime.fromisoformat(observed)
    prior = metadata.get("_refresh", {})
    prior_full = prior.get("last_full_scan_completed_at")
    if datetime.fromisoformat(metadata["captured_to"]) > stamp:
        raise ContractError("baseline_from_future")
    full = (mode == "full" or not prior_full or prior.get("full_scan_incomplete", True)
            or stamp - datetime.fromisoformat(prior_full) >= timedelta(hours=full_interval_hours))
    result = deepcopy(metadata)
    result.update(schema_version="1.0.0", export_id=str(uuid4()), captured_to=observed,
                  exported_at=observed,
                  hour_bucket=stamp.astimezone(timezone(timedelta(hours=8))).strftime(
                      "%Y-%m-%dT%H:00:00+08:00"))
    result["coverage"] = {"status": "partial", "main_pagination": "not_started",
        "replies_pagination": "not_started", "context_status": "no_known_gaps", "reasons": []}
    result["_refresh"] = {
        "version": 1, "requested_mode": mode, "mode": "full" if full else "incremental",
        "started_at": observed, "last_full_scan_completed_at": prior_full,
        "full_scan_incomplete": full or prior.get("full_scan_incomplete", False),
        "baseline_digest": digest({"records": records, "metadata": metadata}),
        "baseline_fingerprints": {row["comment_id"]: fingerprints(row) for row in records},
        "observed_ids": [], "observed_main_ids": [],
    }
    states = {}
    old_rows = {row["comment_id"]: row for row in records}
    for root in sorted({row["root_id"] for row in records} | set(metadata.get("_threads", {})), key=int):
        old = metadata.get("_threads", {}).get(root, {})
        check = old.get("reply_check_state")
        if check not in {"complete", "needs_check", "source_unavailable"}:
            check = "complete" if old.get("pagination_status") == "verified" else "needs_check"
        states[root] = {key: deepcopy(old[key]) for key in (
            "checked_count", "checked_root", "last_complete_at", "last_reply_checked_at",
        ) if key in old}
        root_row = old_rows.get(root)
        evidence = validate_tail_evidence(old.get("tail_evidence"), root_row, old_rows,
                                          snapshot_at=metadata["captured_to"]) if root_row else None
        if evidence is not None:
            states[root]["tail_evidence"] = evidence
        states[root].update(reply_check_state=check, pagination_status="not_started",
                            count=old.get("count"), reply_verification="not_checked")
        if old.get("unavailable"):
            states[root]["prior_unavailable"] = deepcopy(old["unavailable"])
            states[root]["reply_check_state"] = "source_unavailable"
    result["_threads"] = states
    seeds = [dict(row, schema_version="1.0.0", export_id=result["export_id"]) for row in records]
    return seeds, result, {"requests": 0, "finished": False, "blocked": False}


def track_rows(metadata, rows, phase):
    info = metadata.get("_refresh")
    if info is None:
        return
    ids = {row["comment_id"] for row in rows}
    info["observed_ids"] = sorted(set(info["observed_ids"]) | ids, key=int)
    if phase == "main":
        info["observed_main_ids"] = sorted(set(info["observed_main_ids"]) | ids, key=int)


def thread_done(state: dict) -> bool:
    """Output can remain partial even after this run finished checking the source."""
    return bool(state.get("tail_completed") or state.get("skip_refresh") or state.get("unavailable")
                or state.get("pagination_status") == "verified"
                or (state.get("reply_check_state") == "complete"
                    and state.get("reply_verification") == "checked_now"))


def select_threads(metadata, old_rows=None):
    info = metadata["_refresh"]
    seen = set(info["observed_main_ids"])
    for root, state in metadata["_threads"].items():
        if state.get("selected_refresh") or state.get("pagination_status") == "verified":
            continue
        state["selected_refresh"] = True
        absent = root not in seen
        state["main_observed"] = not absent
        complete = state.get("reply_check_state") == "complete"
        latest_count = state.get("checked_count")
        root_row = (old_rows or {}).get(root)
        evidence = validate_tail_evidence(state.get("tail_evidence"), root_row, old_rows,
            snapshot_at=metadata["captured_to"]) if root_row else None
        if evidence is not None:
            latest_count = evidence["source_count"]
        else:
            state.pop("tail_evidence", None)
        unchanged = (type(state.get("main_count")) is int and
                     latest_count == state["main_count"] and
                     state.get("checked_root") == root_signature(state["main_core"])) if not absent else False
        skip = info["mode"] == "incremental" and (
            (complete and (absent or unchanged)) or (absent and state.get("prior_unavailable"))
        )
        if skip:
            state.update(skip_refresh=True, refresh_action="skip", pagination_status="partial",
                         reply_verification="reused_unverified")
            if state.get("prior_unavailable"):
                state["unavailable"] = state["prior_unavailable"]
                state["reply_verification"] = "source_unavailable"
        else:
            plan = select_tail_plan(state, root_row, old_rows, info["mode"],
                                    state.get("main_count")) if root_row else None
            if plan is not None:
                state["refresh_action"] = "tail"
            else:
                state.update(refresh_action="full", reply_check_state="needs_check",
                             reply_verification="not_checked")


def checked_thread(state, root_row, observed):
    state.update(reply_check_state="complete", reply_verification="checked_now",
                 last_reply_checked_at=observed, last_complete_at=observed,
                 checked_count=state["count"], checked_root=root_signature(root_row))
    state.pop("prior_unavailable", None)


def finish_tracking(metadata, records, progress):
    info = metadata.get("_refresh")
    if info is None:
        return
    observed = set(info["observed_ids"])
    reasons = set(metadata["coverage"].get("reasons", []))
    if any(row["kind"] == "root" and row["comment_id"] not in observed for row in records):
        metadata["coverage"]["main_pagination"] = "partial"
        reasons.add("main_incomplete")
    inherited_roots = {row["root_id"] for row in records
                       if row["kind"] == "reply" and row["comment_id"] not in observed}
    for root, state in metadata["_threads"].items():
        if root in inherited_roots or state.get("skip_refresh") or state.get("tail_completed"):
            state["pagination_status"] = "partial"
        if state.get("unavailable"):
            state.update(reply_check_state="source_unavailable", reply_verification="source_unavailable")
        if state["pagination_status"] != "verified":
            reasons.add("replies_incomplete")
    if "replies_incomplete" in reasons:
        metadata["coverage"]["replies_pagination"] = "partial"
    if metadata["coverage"]["main_pagination"] != "verified" or "replies_incomplete" in reasons:
        metadata["coverage"]["status"] = "partial"
    metadata["coverage"]["reasons"] = sorted(reasons)
    if reasons:
        metadata["coverage"]["context_status"] = "gaps"
    if info["mode"] == "full" and progress.get("finished") and progress.get("main_done"):
        info["last_full_scan_completed_at"] = metadata["captured_to"]
        info["full_scan_incomplete"] = False
    baseline = info["baseline_fingerprints"]
    stats = {"new_comments": 0, "changed_payloads": 0, "metadata_updates": 0, "reused_payloads": 0,
             "reused_unverified_comments": 0, "source_requests": progress.get("requests", 0)}
    for row in records:
        cid = row["comment_id"]
        before = baseline.get(cid)
        if before is None:
            stats["new_comments"] += 1
        else:
            after = fingerprints(row)
            stats["changed_payloads" if before["body"] != after["body"] else "reused_payloads"] += 1
            if cid in observed and before["metadata"] != after["metadata"]:
                stats["metadata_updates"] += 1
            if cid not in observed:
                stats["reused_unverified_comments"] += 1
    info["stats"] = stats
