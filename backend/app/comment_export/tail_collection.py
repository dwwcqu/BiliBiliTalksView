"""Guarded tail requests with isolated candidates and one durable full fallback."""

from dataclasses import asdict
from datetime import UTC, datetime

import httpx

from .checkpoint import Checkpoint
from .incremental import track_rows
from .normalization import normalize_comment
from .source import CollectionStopped, fetch_replies
from .tail import TailMismatch, TailPlan, validate_tail_page, validate_tail_pages


def run_tail(plan: TailPlan, source: dict, root_row: dict, old_rows: dict[str, dict],
             cp: Checkpoint, client: httpx.Client, progress: dict, *,
             normalize=None, fetch=None, checked_at=None, recovery=False) -> bool:
    """Return true only after atomic promotion; control/source failures propagate."""
    metadata = progress["metadata"]
    state = metadata["_threads"][plan.root_id]
    clock = checked_at or (lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    fetch = fetch or fetch_replies
    if normalize is None:
        def normalize(raw, root):
            return normalize_comment(raw, root, metadata["video_id"],
                                     metadata["export_id"], clock())
    unclassified_before = list(metadata.get("_unclassified", []))
    attempt = cp.begin_tail(plan.root_id, asdict(plan), progress)
    seen_ids = set()
    previous_created_at = None
    try:
        for page in range(plan.start_page, plan.last_page + 1):
            if page > 1000:
                raise CollectionStopped("budget_exhausted")
            # Record the exact requested page for existing unavailable-target validation.
            state["page"] = page
            data = fetch(source, plan.root_id, page, client)
            payload = {"page": data["page"], "root": normalize(data["root"], plan.root_id),
                       "replies": [normalize(raw, plan.root_id) for raw in data["replies"] or []]}
            page_rows = validate_tail_page(
                plan, payload, page, old_rows, root_row, clock(),
                seen_ids=seen_ids, previous_created_at=previous_created_at)
            guard = getattr(client, "collection_guard", None)
            if guard is not None:
                guard()
            cp.stage_tail_page(plan.root_id, attempt, page, payload, progress)
            seen_ids.update(row["comment_id"] for row in page_rows)
            if page_rows:
                previous_created_at = page_rows[-1]["created_at"]
        pages = cp.read_tail_pages(plan.root_id, attempt)
        rows, evidence = validate_tail_pages(plan, pages, old_rows, root_row,
                                             clock(), state["tail_evidence"])
        rows = [pages[-1]["root"], *rows]
    except (TailMismatch, CollectionStopped) as exc:
        metadata["_unclassified"] = unclassified_before
        if isinstance(exc, CollectionStopped) and str(exc) not in {
                "invalid_response", "video_identity_mismatch"}:
            raise
        for key in ("page", "pass", "seen", "ordered_seen", "first_count", "changed",
                    "observed_core", "baseline", "tail_completed"):
            state.pop(key, None)
        state.update(refresh_action="full", tail_fallback_reason=str(exc), tail_disabled=True,
                     reply_check_state="needs_check", reply_verification="not_checked",
                     pagination_status="not_started")
        metadata["_unclassified"] = unclassified_before
        progress["metadata"] = metadata
        cp.abandon_tail(plan.root_id, attempt, progress, confirmed_progress=progress)
        return False
    finally:
        metadata["_unclassified"] = unclassified_before
    guard = getattr(client, "collection_guard", None)
    if guard is not None:
        guard()
    state.update(tail_completed=True, refresh_action="tail", pagination_status="partial",
                 reply_verification="reused_unverified", tail_evidence=evidence,
                 count=plan.new_count)
    track_rows(metadata, rows, "replies")
    if recovery:
        progress.update(blocked=False, stopped_reason=None, failure=None)
        metadata["coverage"]["reasons"] = [reason for reason in metadata["coverage"]["reasons"]
                                            if reason != "access_restricted"]
    progress["metadata"] = metadata
    cp.promote_tail(plan.root_id, attempt, rows, progress)
    old_rows.update({row["comment_id"]: row for row in rows})
    return True
