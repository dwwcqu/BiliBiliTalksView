"""Serial, resumable collection; frozen data is handed to the exporter."""
import re
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from .access_control import AccessControlError
from .availability import annotate_unavailable
from .checkpoint import Checkpoint
from .contract import ContractError
from .diagnostics import FailureDetail, is_reply_unavailable
from .incremental import (
    checked_thread,
    finish_tracking,
    select_threads,
    start_tracking,
    thread_done,
    track_rows,
)
from .normalization import external_id, normalize_comment
from .publication import exclusive_lock, lock_owned
from .recovery_gate import authorize
from .request_budget import task_budget
from .source import (
    CollectionStopped,
    fetch_main,
    fetch_replies,
    get_signing_keys,
    resolve_video,
)
from .tail import build_tail_evidence, select_tail_plan
from .tail_collection import run_tail
from .zero_reply import (
    build_zero_evidence,
    can_omit,
    merge_history,
    merge_observations,
    observe_zero,
)
from .zero_schedule import decide_policy, validate_policy_snapshot


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _core(row: dict) -> dict:
    return {key: row[key] for key in ("comment_id", "root_id", "parent_id", "author", "content")}


def collect(url: str, work_dir: Path, client: httpx.Client, max_requests: int = 12000,
            resume: bool = False, *, recovery_proof=None, requested_mode="full") -> tuple[list[dict], dict]:
    work_dir = Path(work_dir)
    lock = work_dir / '.collect.lock'
    with nullcontext() if lock_owned(lock) else exclusive_lock(lock):
        return _collect_locked(url, work_dir, client, max_requests, resume,
                               recovery_proof=recovery_proof, requested_mode=requested_mode)


def _collect_locked(url, work_dir, client, max_requests, resume, *, recovery_proof,
                    requested_mode):
    work_dir.mkdir(parents=True, exist_ok=True)
    cp = Checkpoint(work_dir / "work.sqlite3")
    records, metadata = cp.freeze()
    progress = cp.get_progress()
    existing = "video_id" in metadata
    if existing and not resume:
        cp.close()
        raise ContractError("checkpoint_exists_use_resume")
    had_snapshot = "zero_policy_snapshot" in progress
    snapshot = validate_policy_snapshot(progress.get("zero_policy_snapshot"))
    saved_snapshot = metadata.get("_refresh", {}).get("zero_policy_snapshot")
    if (saved_snapshot is not None and validate_policy_snapshot(saved_snapshot) != snapshot
            or snapshot is not None and progress.get("job_id") is not None
            and snapshot["work_id"] != progress["job_id"]):
        snapshot = None
    if snapshot is None:
        progress.pop("zero_policy_snapshot", None)
        refresh_info = metadata.get("_refresh", {})
        refresh_info.pop("zero_policy_snapshot", None)
        refresh_info.pop("zero_reply_schedule", None)
        for state in metadata.get("_threads", {}).values():
            state.pop("zero_completed", None)
            state.pop("zero_reply_evidence", None)
        if existing:
            progress["metadata"] = metadata
    else:
        progress["zero_policy_snapshot"] = snapshot
    if not had_snapshot and "zero_policy_snapshot" not in progress and not resume:
        # Original collect stays strict; refresh entry points freeze their own policy.
        progress["zero_policy_snapshot"] = decide_policy(
            "full", now(), 24, metadata if existing else None,
            work_id=str(progress.get("job_id") or uuid4()), baseline_binding=None)
    progress.setdefault("zero_baseline_root_ids", sorted(metadata.get("_threads", {}), key=int))
    cp.set_progress(progress)
    recovery_pending = bool(progress.get("blocked"))
    if recovery_pending:
        if recovery_proof is None:
            cp.close()
            raise CollectionStopped("blocked_requires_revalidation")
        try:
            authorize(recovery_proof, work_dir, client, progress)
        except CollectionStopped:
            cp.close()
            raise
    if existing and progress.get("finished"):
        cp.close()
        return records, metadata
    progress.setdefault("requests", 0)
    progress.setdefault("max_requests", max_requests)
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc == "www.bilibili.com":
        progress["input_url"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    cp.set_progress(progress)
    budget_scope = task_budget(cp, client, max_requests)
    budget_scope.__enter__()

    def commit_page(key, rows):
        nonlocal recovery_pending
        guard = getattr(client, "collection_guard", None)
        if guard is not None:
            guard()
        track_rows(metadata, rows, "main" if key.startswith("main:") else "replies")
        if recovery_pending:
            before_revision = cp.get_progress().get("checkpoint_revision", 0)
            candidate = dict(progress)
            candidate.update(blocked=False, stopped_reason=None, failure=None)
            metadata["coverage"]["reasons"] = [r for r in metadata["coverage"].get("reasons", [])
                                                if r != "access_restricted"]
            candidate["metadata"] = metadata
            cp.commit_page(key, rows, candidate)
            saved = cp.get_progress()
            if saved.get("checkpoint_revision", 0) != before_revision + 1:
                raise CollectionStopped("recovery_page_not_committed")
            progress.clear()
            progress.update(saved)
            recovery_pending = False
        else:
            cp.commit_page(key, rows, progress)

    try:
        resolved = resolve_video(url, client)
        if existing and metadata["video_id"] != "bilibili:video:" + resolved["source"]["aid"]:
            raise ContractError("checkpoint_video_mismatch")
        if not existing:
            observed = now()
            metadata = {"schema_version": "1.0.0", "export_id": str(uuid4()),
                "video_id": "bilibili:video:" + resolved["source"]["aid"], **resolved,
                "input_url": url, "captured_from": observed, "captured_to": observed,
                "exported_at": observed,
                "hour_bucket": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:00:00+08:00"),
                "source_reported_count": None,
                "coverage": {"status": "partial", "main_pagination": "not_started",
                             "replies_pagination": "not_started", "context_status": "no_known_gaps",
                             "reasons": []}, "_threads": {}, "_unclassified": []}
            cp.initialize(metadata)
        start_tracking(metadata, requested_mode)
        if "zero_policy_snapshot" in progress:
            metadata["_refresh"]["zero_policy_snapshot"] = progress["zero_policy_snapshot"]
        context = getattr(client, "collection_context", None)
        if context is not None:
            metadata["hour_bucket"] = context["hour_bucket"]
        progress["metadata"] = metadata
        cp.set_progress(progress)
        source = metadata["source"]

        def normalize(raw, root=None):
            try:
                return normalize_comment(raw, root, metadata["video_id"], metadata["export_id"], now())
            except ContractError as exc:
                reason = str(exc)
                if reason not in {"invalid_comment_id", "invalid_root_id"}:
                    raise CollectionStopped("invalid_response") from exc
                content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
                member = raw.get("member") if isinstance(raw.get("member"), dict) else {}
                metadata["_unclassified"].append({"reason": reason, "observed_at": now(),
                    "source_comment_id": str(raw["rpid"]) if type(raw.get("rpid")) in (str, int) else None,
                    "source_root_id": str(raw["root"]) if type(raw.get("root")) in (str, int) else None,
                    "author": {"uid": external_id(member.get("mid")),
                               "nickname": member.get("uname") if isinstance(member.get("uname"), str) else None},
                    "content": {"text": content.get("message") if isinstance(content.get("message"), str) else None,
                                "images": [], "emotes": []}})
                return None

        if not progress.get("main_done"):
            keys = get_signing_keys(client)
            seen = set(metadata["_refresh"]["observed_main_ids"])
            cursor = progress.get("cursor", "")
            while True:
                if cursor in progress.get("visited", []):
                    raise CollectionStopped("cursor_cycle")
                data = fetch_main(source, cursor, client, keys)
                raw_rows = list(data["replies"] or [])
                raw_rows += data.get("top_replies") or []
                top = data.get("top") or {}
                if isinstance(top, dict):
                    raw_rows += [row for row in top.values() if isinstance(row, dict) and "rpid" in row]
                rows = []
                for raw in raw_rows:
                    row = normalize(raw)
                    if row:
                        if row["kind"] != "root":
                            raise CollectionStopped("invalid_main_root")
                        rows.append(row)
                        state = metadata["_threads"].setdefault(row["root_id"],
                            {"pagination_status": "not_started", "count": None,
                             "reply_check_state": "needs_check"})
                        incoming = observe_zero(raw, source, now())
                        state["zero_main_observation"] = merge_observations(
                            state.get("zero_main_observation"), incoming)
                        state["zero_reply_history"] = merge_history(
                            state.get("zero_reply_history"),
                            is_new=row["root_id"] not in progress["zero_baseline_root_ids"],
                            nonzero=incoming["nonzero_observed"], unavailable=False)
                        state.update(main_count=raw.get("rcount"), main_core=_core(row))
                new = {r["comment_id"] for r in rows} - seen
                seen.update(r["comment_id"] for r in rows)
                end = data["cursor"]["is_end"]
                if not rows and not end:
                    raise CollectionStopped("unexpected_empty_main")
                progress["old_pages"] = 0 if new else progress.get("old_pages", 0) + 1
                if progress["old_pages"] >= 5 and not end:
                    raise CollectionStopped("repeated_main_pages")
                progress.setdefault("visited", []).append(cursor)
                progress["cursor"] = data["cursor"].get("pagination_reply", {}).get("next_offset", "")
                progress["main_done"] = end
                reported = data["cursor"].get("all_count")
                metadata["source_reported_count"] = reported if type(reported) is int and reported >= 0 else None
                metadata["coverage"]["main_pagination"] = "verified" if end else "partial"
                # Metadata is recoverable from comments/progress if a crash precedes its write.
                progress["metadata"] = metadata
                commit_page("main:" + str(len(progress["visited"])), rows)
                if end:
                    break
                cursor = progress["cursor"]
        records, _ = cp.freeze()
        for row in records:
            metadata["_threads"].setdefault(row["root_id"], {"pagination_status": "not_started", "count": None})
        old_rows = {row["comment_id"]: row for row in records}
        replies_by_root = {}
        for row in records:
            if row["kind"] == "reply":
                replies_by_root.setdefault(row["root_id"], {})[row["comment_id"]] = row
        select_threads(metadata, old_rows)
        progress["metadata"] = metadata
        cp.set_progress(progress)
        for root, state in metadata["_threads"].items():
            if thread_done(state):
                continue
            if (state.get("refresh_action") == "full" and state.get("main_observed")
                    and can_omit(state.get("zero_main_observation"),
                        state.get("zero_reply_history"),
                        stored_replies=len(replies_by_root.get(root, {})),
                        policy=progress.get("zero_policy_snapshot", {}).get("zero_policy", "verify"))):
                evidence = build_zero_evidence(state["zero_main_observation"],
                    state["zero_reply_history"], old_rows[root],
                    progress["zero_policy_snapshot"]["work_id"])
                if evidence is not None:
                    state.update(zero_completed=True, zero_reply_evidence=evidence,
                        refresh_action="zero", count=0, pagination_status="partial",
                        reply_verification="not_checked")
                    progress["metadata"] = metadata
                    commit_page(f"zero:{root}", [])
                    continue
            if state.get("refresh_action") == "tail":
                plan = select_tail_plan(state, old_rows.get(root), old_rows,
                                        metadata["_refresh"]["mode"], state.get("main_count"))
                if plan is not None:
                    try:
                        completed = run_tail(plan, source, old_rows[root], old_rows, cp,
                                             client, progress, normalize=normalize,
                                             fetch=fetch_replies, checked_at=now,
                                             recovery=recovery_pending)
                    except CollectionStopped as exc:
                        if not is_reply_unavailable(exc.detail):
                            raise
                        annotate_unavailable(metadata, root, state["page"], exc.detail)
                        progress["metadata"] = metadata
                        commit_page(f"unavailable:tail:{root}", [])
                        continue
                    if completed:
                        recovery_pending = False
                        continue
                else:
                    state.update(refresh_action="full", reply_check_state="needs_check")
            page = state.get("page", 1)
            pass_number = state.get("pass", 0)
            seen_replies = set(state.get("seen", []))
            floor_rows = replies_by_root.get(root, {}).copy()
            # Older resumed checkpoints have no trustworthy source order.
            ordered_replies = state.get("ordered_seen", [] if page == 1 else None)
            while True:
                if page > 1000:
                    raise CollectionStopped("budget_exhausted")
                try:
                    data = fetch_replies(source, root, page, client)
                except CollectionStopped as exc:
                    if not is_reply_unavailable(exc.detail):
                        raise
                    annotate_unavailable(metadata, root, page, exc.detail)
                    progress["metadata"] = metadata
                    commit_page(f"unavailable:{root}:{pass_number}:{page}", [])
                    break
                state["reply_verification"] = "checked_now"
                state["last_reply_checked_at"] = now()
                count = data["page"]["count"]
                state["zero_reply_history"] = merge_history(
                    state.get("zero_reply_history"), is_new=False,
                    nonzero=count > 0 or bool(data.get("replies")), unavailable=False)
                if "first_count" not in state:
                    state["first_count"] = count
                    main_count = state.get("main_count")
                    if type(main_count) is int and main_count != count:
                        state["changed"] = True
                if count != state["first_count"]:
                    state["changed"] = True
                rows = []
                root_row = normalize(data["root"], root)
                if root_row:
                    rows.append(root_row)
                    prior_core = state.get("observed_core", {}).get(root)
                    if prior_core is None and not pass_number:
                        prior_core = state.get("main_core")
                    if prior_core is not None and prior_core != _core(root_row):
                        state["changed"] = True
                reply_rows = []
                for raw in data["replies"] or []:
                    row = normalize(raw, root)
                    if row:
                        if row["kind"] != "reply":
                            raise CollectionStopped("invalid_reply")
                        reply_rows.append(row)
                floor_rows.update({r["comment_id"]: r for r in reply_rows})
                ids = {r["comment_id"] for r in reply_rows}
                if len(ids) != len(reply_rows) or (ids and not ids - seen_replies):
                    raise CollectionStopped("repeated_reply_page")
                seen_replies.update(ids)
                if (data["page"]["size"] != 20 or data["page"]["num"] != page
                        or len(reply_rows) != min(20, max(0, count - (page - 1) * 20))):
                    ordered_replies = None
                    state["ordered_seen"] = None
                if ordered_replies is not None:
                    ordered_replies.extend(row["comment_id"] for row in reply_rows)
                    state["ordered_seen"] = ordered_replies
                if len(seen_replies) > count:
                    raise CollectionStopped("reply_count_mismatch")
                tail = page * data["page"]["size"] >= count
                if not ids and not tail:
                    raise CollectionStopped("unexpected_empty_replies")
                rows += reply_rows
                state.update(page=page + 1, count=count, seen=sorted(seen_replies),
                             pagination_status="partial", **{"pass": pass_number})
                observed_core = state.setdefault("observed_core", {})
                observed_core.update({row["comment_id"]: _core(row) for row in rows})
                restart = False
                if tail:
                    if len(seen_replies) != count:
                        raise CollectionStopped("reply_count_mismatch")
                    if pass_number:
                        if state.get("changed") or observed_core != state.get("baseline"):
                            raise CollectionStopped("count_changed")
                    elif state.get("changed"):
                        state.update(baseline=observed_core, observed_core={}, changed=False,
                                     page=1, seen=[], ordered_seen=[], first_count=count, **{"pass": 1})
                        restart = True
                    if not restart:
                        state["pagination_status"] = "verified"
                        if root_row:
                            checked_thread(state, root_row, now())
                            evidence = None
                            # Small floors always use full reads; they need no tail anchors.
                            if (count > 60 and ordered_replies is not None
                                    and len(floor_rows) == count
                                    and not metadata["_unclassified"]):
                                evidence = build_tail_evidence(
                                    [floor_rows[cid] for cid in ordered_replies], root_row,
                                    count, state["last_complete_at"])
                            state.pop("tail_evidence", None)
                            if evidence is not None:
                                state["tail_evidence"] = evidence
                        state.pop("ordered_seen", None)
                        state.pop("baseline", None)
                # Tail verification or a re-read transition shares the page transaction.
                progress["metadata"] = metadata
                commit_page(f"reply:{root}:{pass_number}:{page}", rows)
                old_rows.update({row["comment_id"]: row for row in rows})
                if restart:
                    pass_number, page, seen_replies = 1, 1, set()
                    ordered_replies = []
                    continue
                if tail:
                    break
                page += 1
        if recovery_pending:
            raise CollectionStopped("recovery_no_data_commit")
        metadata["coverage"].update(status="verified", replies_pagination="verified", reasons=[])
        if any(state.get("unavailable") for state in metadata["_threads"].values()):
            metadata["coverage"].update(status="partial", replies_pagination="partial",
                                        reasons=["replies_incomplete"], context_status="gaps")
        if metadata["_unclassified"]:
            metadata["coverage"]["status"] = "partial"
            metadata["coverage"]["reasons"] = sorted(set(metadata["coverage"]["reasons"]) | {"unclassified_record"})
        progress["finished"] = True
        progress["blocked"] = False
        progress["stopped_reason"] = None
        progress["failure"] = None
    except Exception as exc:
        reason = (getattr(exc, "reason", str(exc))
                  if isinstance(exc, (CollectionStopped, ContractError, AccessControlError)) else "internal_error")
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", reason):
            reason = "internal_error"
        # Discard uncommitted page state; request debits are already durable.
        records, metadata = cp.freeze()
        progress = cp.get_progress()
        detail = getattr(exc, "detail", None)
        control_stop = reason in {"cooldown_active", "source_busy", "clock_inconsistent", "budget_exhausted", "lease_lost", "job_cancelled", "worker_stopped"}
        if not control_stop:
            if detail is None:
                attempt = progress.get("attempt") or {}
                detail = FailureDetail(category="local_validation", safe_reason=reason,
                    video_id=metadata.get("video_id"), target=attempt.get("target", {}))
                if reason == "access_restricted":
                    detail = replace(detail, category="access_restricted",
                                     phase=attempt.get("phase", "local_validation"), endpoint=attempt.get("endpoint"))
            detail = replace(detail, checkpoint_revision=progress.get("checkpoint_revision", 0))
            progress["failure"] = detail.to_dict()
        resumable = (reason in {"network_error", "budget_exhausted", "cooldown_active", "source_busy", "lease_lost", "job_cancelled", "worker_stopped"}
                     or (detail is not None and detail.category in {"network_error", "source_unavailable"}))
        progress["stopped_reason"] = reason
        progress["blocked"] = bool(progress.get("blocked")) or not resumable
        if "video_id" not in metadata:
            cp.set_progress(progress)
            raise
        coverage = metadata["coverage"]
        coverage["status"] = "partial"
        reasons = set(coverage["reasons"])
        reasons.add("main_incomplete" if not progress.get("main_done") else "replies_incomplete")
        if reason in {"access_restricted", "budget_exhausted", "count_changed", "identity_conflict"}:
            reasons.add(reason)
        coverage["reasons"] = sorted(reasons)
    finally:
        budget_scope.__exit__(None, None, None)
        cp.close()
    cp = Checkpoint(work_dir / "work.sqlite3")
    try:
        if "video_id" in metadata:
            metadata["captured_to"] = now()
            metadata["exported_at"] = now()
            current_rows, _ = cp.freeze()
            finish_tracking(metadata, current_rows, progress)
            progress["metadata"] = metadata
        cp.set_progress(progress)
        records, final_metadata = cp.freeze()
    finally:
        cp.close()
    return records, final_metadata
