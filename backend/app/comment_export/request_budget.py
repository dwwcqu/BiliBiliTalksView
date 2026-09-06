"""One task request debit, shared by normal collection and bounded probes."""

import time
from contextlib import contextmanager

from .diagnostics import ENDPOINT_PHASES, FailureDetail
from .source import CollectionStopped, _request_target


@contextmanager
def task_budget(cp, client, max_requests: int, *, call_limit: int | None = None):
    used = 0
    last = 0.0

    def debit(request):
        nonlocal used, last
        guard = getattr(client, "collection_guard", None)
        if guard is not None:
            guard()
        if request.url.scheme != "https" or request.url.host != "api.bilibili.com":
            raise CollectionStopped("unsafe_request")
        progress = cp.get_progress()
        budget = min(max_requests, progress.get("max_requests", max_requests))
        if progress.get("requests", 0) >= budget:
            raise CollectionStopped("budget_exhausted")
        if call_limit is not None and used >= call_limit:
            raise CollectionStopped("probe_request_limit")
        if not hasattr(client, "access_policy"):
            time.sleep(max(0, 2 - (time.monotonic() - last)))
        video_id, target = _request_target(request.url.path, dict(request.url.params))
        prior = getattr(client, "last_diagnostic", None)
        if isinstance(prior, FailureDetail) and prior.endpoint == request.url.path:
            target = prior.target
        progress["attempt"] = FailureDetail(
            phase=ENDPOINT_PHASES.get(request.url.path, "local_validation"),
            endpoint=request.url.path,
            video_id=video_id,
            target=target,
            checkpoint_revision=progress.get("checkpoint_revision", 0),
            safe_reason="request_attempt",
        ).to_dict()
        progress["requests"] = progress.get("requests", 0) + 1
        progress["max_requests"] = budget
        cp.set_progress(progress)
        used += 1
        last = time.monotonic()

    client.event_hooks["request"].append(debit)
    try:
        yield
    finally:
        client.event_hooks["request"].remove(debit)
