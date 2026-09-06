"""A source-unavailable floor is a retained gap, never a completed empty floor."""
from .contract import ContractError
from .diagnostics import is_reply_unavailable
from .incremental import thread_done
from .zero_reply import merge_history


def annotate_unavailable(metadata, root, page, detail):
    if (not is_reply_unavailable(detail) or detail.phase != "replies"
            or detail.target != {"root_id": root, "page": page}
            or detail.video_id != metadata["video_id"]):
        raise ContractError("unavailable_target_mismatch")
    state = metadata["_threads"][root]
    if state.get("page", 1) != page or state.get("pagination_status") == "verified":
        raise ContractError("unavailable_target_stale")
    state["unavailable"] = {"http_status": 200, "api_code": detail.api_code,
                            "observed_at": detail.observed_at, "page": page}
    state["zero_reply_history"] = merge_history(
        state.get("zero_reply_history"), is_new=False, nonzero=False, unavailable=True)
    state["pagination_status"] = "partial"
    state["reply_check_state"] = "source_unavailable"
    state["reply_verification"] = "source_unavailable"
    state["reasons"] = sorted(set(state.get("reasons", [])) | {"replies_incomplete"})
    coverage = metadata["coverage"]
    coverage.update(status="partial", replies_pagination="partial", context_status="gaps")
    coverage["reasons"] = sorted((set(coverage.get("reasons", [])) - {"access_restricted"})
                                | {"replies_incomplete"})


def commit_unavailable(cp, detail):
    _rows, metadata = cp.freeze()
    progress = cp.get_progress()
    if detail.checkpoint_revision != progress.get("checkpoint_revision", 0):
        raise ContractError("unavailable_target_stale")
    pending = next(((root, state) for root, state in metadata["_threads"].items()
                    if not thread_done(state)), None)
    if pending is None or detail.target.get("root_id") != pending[0]:
        raise ContractError("unavailable_target_stale")
    root, state = pending
    page = detail.target["page"]
    annotate_unavailable(metadata, root, page, detail)
    progress.update(metadata=metadata, blocked=False, stopped_reason=None, failure=None)
    cp.commit_page(f"unavailable:{root}:{state.get('pass', 0)}:{page}", [], progress)
