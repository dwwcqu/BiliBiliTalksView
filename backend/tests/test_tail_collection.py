from copy import deepcopy

import httpx
import pytest
from test_incremental_collection import next_collection, raw

from app.comment_export import collector
from app.comment_export.checkpoint import read_checkpoint


def install_paged_source(monkeypatch, count, *, mutate=None):
    monkeypatch.setattr(collector, "resolve_video", lambda *args: {
        "source": {"platform": "bilibili", "aid": "1", "oid": "1", "bvid": "BVfake",
                   "episode_id": None, "comment_type": 1},
        "canonical_url": "https://www.bilibili.com/video/BVfake", "title": "sample"})
    monkeypatch.setattr(collector, "get_signing_keys", lambda client: ("a", "b"))
    root = raw(100, count=count)
    monkeypatch.setattr(collector, "fetch_main", lambda *args: {
        "replies": [root], "cursor": {"is_end": True, "all_count": count + 1}})
    calls = []

    def replies(source, rid, page, client):
        calls.append(page)
        data = {"root": deepcopy(root), "page": {"num": page, "size": 20, "count": count},
                "replies": [dict(raw(1000 + i, 100, text=f"reply {i}"), ctime=i + 1)
                            for i in range((page - 1) * 20, min(page * 20, count))]}
        if mutate:
            mutate(data, calls)
        return data

    monkeypatch.setattr(collector, "fetch_replies", replies)
    return calls


def test_full_scan_builds_ordered_tail_evidence(monkeypatch, tmp_path):
    calls = install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        _, meta = collector.collect("url", tmp_path, client)
    assert calls == list(range(1, 21))
    evidence = meta["_threads"]["100"].get("tail_evidence")
    assert evidence is not None
    assert evidence["anchor_ids"] == [str(1000 + i) for i in range(360, 397)]
    assert evidence["anchor_start_page"] == 19
    assert evidence["source_count"] == 397
    assert "ordered_seen" not in meta["_threads"]["100"]


@pytest.mark.parametrize("old,new,pages", [(397, 398, [19, 20]), (400, 401, [19, 20, 21])])
def test_tail_append_matches_full_source(monkeypatch, tmp_path, old, new, pages):
    install_paged_source(monkeypatch, old)
    monkeypatch.setattr(collector, "now", lambda: "2026-09-05T09:00:00Z")
    with httpx.Client() as client:
        baseline, before = collector.collect("url", tmp_path / "base", client)
        monkeypatch.setattr(collector, "now", lambda: "2026-09-05T10:00:00Z")
        calls = install_paged_source(monkeypatch, new)
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
        assert calls == pages
        full, _ = collector.collect("url", tmp_path / "full", client)
    comparable = lambda values: [{k: v for k, v in r.items()
                                  if k not in {"export_id", "collected_at"}} for r in values]
    assert comparable(rows) == comparable(full)
    state = meta["_threads"]["100"]
    assert state["tail_completed"]
    assert state["checked_count"] == old
    assert state["tail_evidence"]["source_count"] == new
    assert state["last_complete_at"] == before["_threads"]["100"]["last_complete_at"]
    assert state["reply_verification"] == "reused_unverified"
    assert meta["coverage"]["status"] == "partial"
    assert rows[1]["collected_at"] == baseline[1]["collected_at"]


def test_interrupted_tail_restarts_anchor_without_publishing_candidates(monkeypatch, tmp_path):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def interrupt(data, calls):
            if calls == [19, 20]:
                raise collector.CollectionStopped("network_error")
            data["replies"][0]["content"]["message"] = "candidate update"

        calls = install_paged_source(monkeypatch, 398, mutate=interrupt)
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
        assert calls == [19, 20]
        assert len(rows) == 398
        assert next(r for r in rows if r["comment_id"] == "1360")["content"]["text"] == "reply 360"
        assert not meta["_threads"]["100"].get("tail_completed")
        collector.collect("url", tmp_path / "next", client, 100, resume=True)
    assert calls == [19, 20, 19, 20]
    rows, meta, _ = read_checkpoint(tmp_path / "next")
    assert len(rows) == 399
    assert meta["_threads"]["100"]["tail_completed"]


def test_tail_mismatch_falls_back_once_from_first_page(monkeypatch, tmp_path):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def mismatch(data, calls):
            if calls[0] == 19 and 1 not in calls:
                data["page"]["count"] = 399

        calls = install_paged_source(monkeypatch, 398, mutate=mismatch)
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls[:2] == [19, 20] or calls[:2] == [19, 1]
    assert calls[-20:] == list(range(1, 21))
    assert len(rows) == 399
    assert meta["_threads"]["100"]["tail_disabled"]
    assert meta["_threads"]["100"]["refresh_action"] == "full"
    assert meta["_threads"]["100"]["reply_verification"] == "checked_now"

@pytest.mark.parametrize("reason", ["job_cancelled", "lease_lost", "access_restricted",
                                    "budget_exhausted", "source_busy"])
def test_tail_control_stop_never_falls_back_or_publishes(monkeypatch, tmp_path, reason):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def stop(data, calls):
            if len(calls) == 2:
                raise collector.CollectionStopped(reason)
            data["replies"][0]["content"]["message"] = "not yet verified"

        calls = install_paged_source(monkeypatch, 398, mutate=stop)
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls == [19, 20]
    assert len(rows) == 398
    assert next(r for r in rows if r["comment_id"] == "1360")["content"]["text"] == "reply 360"
    assert not meta["_threads"]["100"].get("tail_disabled")
    assert not meta["_threads"]["100"].get("tail_completed")


def test_second_append_then_unchanged_reuses_latest_tail_count(monkeypatch, tmp_path):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        calls = install_paged_source(monkeypatch, 398)
        next_collection(tmp_path / "base", tmp_path / "one", client)
        assert calls == [19, 20]
        calls = install_paged_source(monkeypatch, 399)
        next_collection(tmp_path / "one", tmp_path / "two", client)
        assert calls == [19, 20]
        calls.clear()
        _, meta = next_collection(tmp_path / "two", tmp_path / "three", client)
    assert calls == []
    state = meta["_threads"]["100"]
    assert state["checked_count"] == 397
    assert state["tail_evidence"]["source_count"] == 399
    assert state["skip_refresh"]
    assert not state.get("tail_completed")


@pytest.mark.parametrize("mutation", ["anchor", "duplicate", "empty", "count", "size",
                                       "root", "backwards", "existing_new", "adapter"])
def test_candidate_mismatches_fallback_with_bounded_requests(monkeypatch, tmp_path, mutation):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def mismatch(data, calls):
            if 1 in calls:
                return
            if mutation == "anchor":
                data["replies"][0]["rpid"] = 9999
            elif mutation == "duplicate":
                data["replies"][1] = deepcopy(data["replies"][0])
            elif mutation == "empty":
                data["replies"] = []
            elif mutation == "count":
                data["page"]["count"] += 1
            elif mutation == "size":
                data["page"]["size"] = 10
            elif mutation == "root":
                data["root"]["member"]["mid"] = 2
            elif mutation == "backwards":
                data["replies"][1]["ctime"] = 0
            elif mutation == "existing_new" and data["page"]["num"] == 20:
                data["replies"][-1]["rpid"] = 1001
            elif mutation == "adapter":
                raise collector.CollectionStopped("invalid_response")

        calls = install_paged_source(monkeypatch, 398, mutate=mismatch)
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls[0] == 19
    assert calls[-20:] == list(range(1, 21))
    assert len(calls) <= 22
    assert len(rows) == 399
    assert meta["_threads"]["100"]["tail_disabled"]


def test_promoted_tail_is_not_replayed_after_interruption(monkeypatch, tmp_path):
    from app.comment_export.checkpoint import Checkpoint

    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        original = Checkpoint.promote_tail

        def promote_then_stop(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise collector.CollectionStopped("network_error")

        monkeypatch.setattr(Checkpoint, "promote_tail", promote_then_stop)
        calls = install_paged_source(monkeypatch, 398)
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
        assert meta["_threads"]["100"]["tail_completed"]
        assert len(rows) == 399
        collector.collect("url", tmp_path / "next", client, 100, resume=True)
    assert calls == [19, 20]


def test_tail_root_observed_metadata_is_merged(monkeypatch, tmp_path):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def update(data, calls):
            data["root"]["like"] = 42

        install_paged_source(monkeypatch, 398, mutate=update)
        rows, _ = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert next(r for r in rows if r["kind"] == "root")["like_count"] == 42


def test_full_nonstandard_page_size_cannot_build_twenty_row_anchors(monkeypatch, tmp_path):
    install_paged_source(monkeypatch, 81)
    root = raw(100, count=81)

    def replies(source, rid, page, client):
        return {"root": root, "page": {"num": page, "size": 10, "count": 81},
                "replies": [raw(1000 + i, 100) for i in range((page - 1) * 10, min(page * 10, 81))]}

    monkeypatch.setattr(collector, "fetch_replies", replies)
    with httpx.Client() as client:
        _, meta = collector.collect("url", tmp_path, client)
    assert "tail_evidence" not in meta["_threads"]["100"]


def test_fallback_decision_survives_crash_at_abandon(monkeypatch, tmp_path):
    from app.comment_export.checkpoint import Checkpoint

    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        original = Checkpoint.abandon_tail

        def abandon_then_stop(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise collector.CollectionStopped("network_error")

        monkeypatch.setattr(Checkpoint, "abandon_tail", abandon_then_stop)

        def mismatch(data, calls):
            if 1 not in calls:
                data["page"]["count"] += 1

        calls = install_paged_source(monkeypatch, 398, mutate=mismatch)
        next_collection(tmp_path / "base", tmp_path / "next", client)
        _, meta, _ = read_checkpoint(tmp_path / "next")
        assert meta["_threads"]["100"]["tail_disabled"]
        collector.collect("url", tmp_path / "next", client, 100, resume=True)
    assert calls == [19, *range(1, 21)]


def test_tail_resume_keeps_cumulative_request_budget(monkeypatch, tmp_path):
    import app.comment_export.request_budget as budget

    install_paged_source(monkeypatch, 397)
    monkeypatch.setattr(budget.time, "sleep", lambda *_: None)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        collector.collect("url", tmp_path / "base", client)

        def debit_and_stop(data, calls):
            client.get("https://api.bilibili.com/x/v2/reply/reply",
                       params={"oid": "1", "root": "100", "pn": data["page"]["num"]})
            if calls == [19, 20]:
                raise collector.CollectionStopped("job_cancelled")

        calls = install_paged_source(monkeypatch, 398, mutate=debit_and_stop)
        next_collection(tmp_path / "base", tmp_path / "next", client)
        _, _, progress = read_checkpoint(tmp_path / "next")
        assert progress["requests"] == 2
        collector.collect("url", tmp_path / "next", client, 1000, resume=True)
    _, meta, progress = read_checkpoint(tmp_path / "next")
    assert calls == [19, 20, 19, 20]
    assert progress["requests"] == 4
    assert progress["max_requests"] == 100
    assert meta["_threads"]["100"]["tail_completed"]


def test_final_full_pass_alone_defines_anchor_order(monkeypatch, tmp_path):
    def equal_times_and_reorder(data, calls):
        for reply in data["replies"]:
            reply["ctime"] = 1
        if len(calls) > 5 and data["page"]["num"] == 4:
            data["replies"][0], data["replies"][1] = data["replies"][1], data["replies"][0]

    calls = install_paged_source(monkeypatch, 81, mutate=equal_times_and_reorder)
    monkeypatch.setattr(collector, "fetch_main", lambda *_: {
        "replies": [raw(100, count=80)], "cursor": {"is_end": True, "all_count": 81}})
    with httpx.Client() as client:
        _, meta = collector.collect("url", tmp_path, client)
    assert calls == [*range(1, 6), *range(1, 6)]
    evidence = meta["_threads"]["100"]["tail_evidence"]
    assert evidence["anchor_ids"][:2] == ["1061", "1060"]
    assert len(evidence["anchor_ids"]) == 21


def test_legacy_resumed_full_read_cannot_invent_source_order(monkeypatch, tmp_path):
    from app.comment_export.checkpoint import Checkpoint

    def stop_second(data, calls):
        if calls == [1, 2]:
            raise collector.CollectionStopped("network_error")

    calls = install_paged_source(monkeypatch, 81, mutate=stop_second)
    with httpx.Client() as client:
        collector.collect("url", tmp_path, client)
        cp = Checkpoint(tmp_path / "work.sqlite3")
        progress = cp.get_progress()
        progress["metadata"]["_threads"]["100"].pop("ordered_seen")
        cp.set_progress(progress)
        cp.close()
        _, meta = collector.collect("url", tmp_path, client, resume=True)
    assert calls == [1, 2, 2, 3, 4, 5]
    assert meta["_threads"]["100"]["reply_check_state"] == "complete"
    assert "tail_evidence" not in meta["_threads"]["100"]


def test_full_evidence_uses_same_check_timestamp(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    tick = datetime(2026, 9, 5, tzinfo=UTC)

    def clock():
        nonlocal tick
        tick += timedelta(seconds=1)
        return tick.strftime("%Y-%m-%dT%H:%M:%SZ")

    install_paged_source(monkeypatch, 81)
    monkeypatch.setattr(collector, "now", clock)
    with httpx.Client() as client:
        _, meta = collector.collect("url", tmp_path, client)
    state = meta["_threads"]["100"]
    assert state["tail_evidence"]["last_full_checked_at"] == state["last_complete_at"]


@pytest.mark.parametrize("fail_page", [19, 20])
def test_unavailable_tail_preserves_confirmed_rows(monkeypatch, tmp_path, fail_page):
    from app.comment_export.diagnostics import FailureDetail

    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def unavailable(data, calls):
            if data["page"]["num"] != fail_page:
                data["replies"][0]["content"]["message"] = "unconfirmed"
            else:
                raise collector.CollectionStopped("source_reply_unavailable", FailureDetail(
                    endpoint="/x/v2/reply/reply", phase="replies", http_status=200, api_code=12022,
                    category="resource_unavailable", video_id="bilibili:video:1",
                    target={"root_id": "100", "page": fail_page}))

        calls = install_paged_source(monkeypatch, 398, mutate=unavailable)
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls == list(range(19, fail_page + 1))
    assert len(rows) == 398
    assert meta["_threads"]["100"]["reply_verification"] == "source_unavailable"
    assert meta["_threads"]["100"]["unavailable"]["page"] == fail_page
    assert meta["_unclassified"] == []
    assert next(r for r in rows if r["comment_id"] == "1360")["content"]["text"] == "reply 360"


@pytest.mark.parametrize("mutation", ["count", "anchor"])
def test_first_tail_page_mismatch_stops_speculative_requests(monkeypatch, tmp_path, mutation):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def mismatch(data, calls):
            if len(calls) == 1:
                if mutation == "count":
                    data["page"]["count"] += 1
                else:
                    data["replies"][0]["rpid"] = 99999

        calls = install_paged_source(monkeypatch, 500, mutate=mismatch)
        _, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls == [19, *range(1, 26)]
    assert meta["_threads"]["100"]["tail_disabled"]


@pytest.mark.parametrize("mutation", ["root_uid", "root_body", "overlap_uid", "parent", "time"])
def test_first_tail_structural_mismatch_falls_back_immediately(monkeypatch, tmp_path, mutation):
    install_paged_source(monkeypatch, 397)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)

        def mismatch(data, calls):
            if len(calls) != 1:
                return
            if mutation == "root_uid":
                data["root"]["member"]["mid"] = 2
            elif mutation == "root_body":
                data["root"]["content"]["message"] = "changed root"
            elif mutation == "overlap_uid":
                data["replies"][0]["member"]["mid"] = 2
            elif mutation == "parent":
                data["replies"][0]["parent"] = 1001
            elif mutation == "time":
                data["replies"][1]["ctime"] = 1

        calls = install_paged_source(monkeypatch, 500, mutate=mismatch)
        _, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls == [19, *range(1, 26)]
    assert meta["_threads"]["100"]["tail_disabled"]
