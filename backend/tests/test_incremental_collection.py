from copy import deepcopy

import httpx

from app.comment_export import collector


def raw(cid, root=0, count=0, text="body"):
    return {"rpid": cid, "root": root, "parent": root,
            "member": {"mid": 1, "uname": "user"}, "content": {"message": text},
            "ctime": 1, "like": 0, "rcount": count}


def install_source(monkeypatch, reply_ids=(101,)):
    monkeypatch.setattr(collector, "resolve_video", lambda *args: {
        "source": {"platform": "bilibili", "aid": "1", "oid": "1", "bvid": "BVfake",
                   "episode_id": None, "comment_type": 1},
        "canonical_url": "https://www.bilibili.com/video/BVfake", "title": "sample"})
    monkeypatch.setattr(collector, "get_signing_keys", lambda client: ("a", "b"))
    root = raw(100, count=len(reply_ids))
    monkeypatch.setattr(collector, "fetch_main", lambda *args: {
        "replies": [root], "cursor": {"is_end": True, "all_count": 1 + len(reply_ids)}})
    calls = []

    def replies(source, rid, page, client):
        calls.append((rid, page))
        return {"root": root, "page": {"num": page, "size": 20, "count": len(reply_ids)},
                "replies": [raw(cid, 100) for cid in reply_ids]}

    monkeypatch.setattr(collector, "fetch_replies", replies)
    return calls


def test_collect_persists_full_check_evidence(monkeypatch, tmp_path):
    install_source(monkeypatch)
    with httpx.Client() as client:
        rows, meta = collector.collect("url", tmp_path, client)
    assert len(rows) == 2
    assert meta["_refresh"]["last_full_scan_completed_at"]
    assert meta["_threads"]["100"]["reply_check_state"] == "complete"


def test_prepare_refresh_preserves_historical_evidence(frozen_case):
    from app.comment_export.incremental import prepare_refresh

    rows, meta = deepcopy(frozen_case)
    meta["_refresh"] = {"version": 1, "last_full_scan_completed_at": "2026-09-05T09:00:00Z",
                        "full_scan_incomplete": False}
    meta["_threads"]["100"].update(reply_check_state="complete", checked_count=1,
        checked_root="digest", last_complete_at="2026-09-05T09:00:00Z")
    seeds, result, progress = prepare_refresh(rows, meta, {}, "auto", "2026-09-05T10:00:00Z", 24)
    assert result["_refresh"]["mode"] == "incremental"
    assert result["_threads"]["100"]["reply_check_state"] == "complete"
    assert result["_threads"]["100"]["pagination_status"] == "not_started"
    assert seeds[0]["collected_at"] == rows[0]["collected_at"]
    assert result["export_id"] != meta["export_id"]
    assert not progress.get("main_done")


def next_collection(base, target, client, mode="auto"):
    from app.comment_export.checkpoint import Checkpoint
    from app.comment_export.incremental import prepare_refresh

    cp = Checkpoint(base / "work.sqlite3")
    try:
        rows, meta = cp.freeze()
        progress = cp.get_progress()
    finally:
        cp.close()
    seeds, meta, progress = prepare_refresh(rows, meta, progress, mode, collector.now(), 24)
    cp = Checkpoint(target / "work.sqlite3")
    try:
        cp.commit_page("refresh:baseline", seeds, {**progress, "metadata": meta, "max_requests": 100})
    finally:
        cp.close()
    return collector.collect("url", target, client, 100, resume=True)


def test_consecutive_fast_refreshes_reuse_complete_floor(monkeypatch, tmp_path):
    calls = install_source(monkeypatch)
    with httpx.Client() as client:
        baseline, _ = collector.collect("url", tmp_path / "base", client)
        calls.clear()
        for source, target in [("base", "one"), ("one", "two")]:
            rows, meta = next_collection(tmp_path / source, tmp_path / target, client)
            assert calls == []
            assert meta["_refresh"]["mode"] == "incremental"
            assert meta["_threads"]["100"]["reply_check_state"] == "complete"
            assert meta["coverage"]["status"] == "partial"
            assert meta["_refresh"]["stats"]["new_comments"] == 0
            assert meta["_refresh"]["stats"]["reused_unverified_comments"] == 1
            assert next(r for r in rows if r["kind"] == "reply")["collected_at"] == baseline[1]["collected_at"]


def test_changed_reply_count_fetches_old_floor(monkeypatch, tmp_path):
    install_source(monkeypatch)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        calls = install_source(monkeypatch, (101, 102))
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls == [("100", 1)]
    assert {r["comment_id"] for r in rows} == {"100", "101", "102"}
    assert meta["coverage"]["status"] == "verified"
    assert meta["_refresh"]["stats"]["new_comments"] == 1


def test_full_keeps_unobserved_reply_and_marks_partial(monkeypatch, tmp_path):
    install_source(monkeypatch, (101, 102))
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        calls = install_source(monkeypatch, (101,))
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client, "full")
    assert calls == [("100", 1)]
    assert {r["comment_id"] for r in rows} == {"100", "101", "102"}
    assert meta["coverage"]["status"] == "partial"
    assert meta["_threads"]["100"]["pagination_status"] == "partial"
    assert meta["_threads"]["100"]["reply_check_state"] == "complete"
    assert meta["_refresh"]["full_scan_incomplete"] is False


def test_old_main_ids_do_not_stop_pagination(monkeypatch, tmp_path):
    install_source(monkeypatch, ())
    roots = [raw(cid) for cid in range(100, 108)]
    monkeypatch.setattr(collector, "fetch_main", lambda *args: {
        "replies": roots, "cursor": {"is_end": True, "all_count": len(roots)}})
    monkeypatch.setattr(collector, "fetch_replies", lambda source, rid, page, client: {
        "root": raw(int(rid)), "page": {"num": page, "size": 20, "count": 0}, "replies": []})
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        calls = []

        def main(source, cursor, *_):
            index = int(cursor or "0")
            calls.append(index)
            return {"replies": [roots[index]], "cursor": {
                "is_end": index == 7, "all_count": 8,
                "pagination_reply": {"next_offset": str(index + 1)}}}

        monkeypatch.setattr(collector, "fetch_main", main)
        _, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert calls == list(range(8))
    assert meta["coverage"]["main_pagination"] == "verified"


def test_unseen_root_retained_and_full_period_enforced(monkeypatch, tmp_path):
    install_source(monkeypatch, ())
    monkeypatch.setattr(collector, "now", lambda: "2026-09-05T09:00:00Z")
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        monkeypatch.setattr(collector, "now", lambda: "2026-09-05T10:00:00Z")
        monkeypatch.setattr(collector, "fetch_main", lambda *_: {
            "replies": [], "cursor": {"is_end": True, "all_count": 0}})
        rows, meta = next_collection(tmp_path / "base", tmp_path / "fast", client)
        assert len(rows) == 1
        assert "main_incomplete" in meta["coverage"]["reasons"]
        monkeypatch.setattr(collector, "now", lambda: "2026-09-06T10:00:00Z")
        calls = install_source(monkeypatch, ())
        _, meta = next_collection(tmp_path / "fast", tmp_path / "full", client)
    assert calls == [("100", 1)]
    assert meta["_refresh"]["mode"] == "full"


def test_internal_evidence_never_leaks_to_export(monkeypatch, tmp_path):
    from app.comment_export.cli import export_work
    from app.comment_export.validation import validate_batch

    install_source(monkeypatch)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        next_collection(tmp_path / "base", tmp_path / "fast", client)
    output, _ = export_work(tmp_path / "fast", tmp_path / "out")
    manifest = validate_batch(output)
    assert "_refresh" not in manifest
    assert manifest["coverage"]["status"] == "partial"
    assert (output / "threads" / "000" / "comments.jsonl").read_text(encoding="utf-8").count("\n") == 2


def test_checkpoint_preserves_escaped_surrogate_baseline(frozen_case, tmp_path):
    from app.comment_export.checkpoint import Checkpoint

    rows, meta = frozen_case
    rows[0]["content"]["text"] = "original\ud800"
    cp = Checkpoint(tmp_path / "work.sqlite3")
    try:
        cp.commit_page("baseline", rows, {"metadata": meta})
        saved, _ = cp.freeze()
    finally:
        cp.close()
    assert saved[0]["content"]["text"] == "original\ud800"


def test_incomplete_floor_resumes_committed_reply_page(monkeypatch, tmp_path):
    install_source(monkeypatch)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        root = raw(100, count=21)
        monkeypatch.setattr(collector, "fetch_main", lambda *_: {
            "replies": [root], "cursor": {"is_end": True, "all_count": 22}})
        calls = []
        failed = False

        def replies(source, rid, page, client):
            nonlocal failed
            calls.append(page)
            if page == 2 and not failed:
                failed = True
                raise collector.CollectionStopped("network_error")
            ids = range(101, 121) if page == 1 else [121]
            return {"root": root, "page": {"num": page, "size": 20, "count": 21},
                    "replies": [raw(cid, 100) for cid in ids]}

        monkeypatch.setattr(collector, "fetch_replies", replies)
        _, partial = next_collection(tmp_path / "base", tmp_path / "next", client)
        assert partial["_threads"]["100"]["reply_check_state"] == "needs_check"
        rows, complete = collector.collect("url", tmp_path / "next", client, 100, resume=True)
    assert calls == [1, 2, 2]
    assert len(rows) == 22
    assert complete["coverage"]["status"] == "verified"


def test_same_count_replacement_discovered_only_by_full(monkeypatch, tmp_path):
    install_source(monkeypatch)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        calls = install_source(monkeypatch, (102,))
        rows, meta = next_collection(tmp_path / "base", tmp_path / "fast", client)
        assert calls == []
        assert {r["comment_id"] for r in rows} == {"100", "101"}
        assert meta["coverage"]["status"] == "partial"
        rows, meta = next_collection(tmp_path / "fast", tmp_path / "full", client, "full")
    assert calls == [("100", 1)]
    assert {r["comment_id"] for r in rows} == {"100", "101", "102"}
    assert meta["coverage"]["status"] == "partial"


def test_unavailable_absent_floor_waits_until_reappearing(monkeypatch, tmp_path):
    from app.comment_export.diagnostics import FailureDetail

    install_source(monkeypatch, ())
    calls = []

    def unavailable(source, rid, page, client):
        calls.append(rid)
        raise collector.CollectionStopped("source_reply_unavailable", FailureDetail(
            endpoint="/x/v2/reply/reply", phase="replies", http_status=200, api_code=12022,
            category="resource_unavailable", video_id="bilibili:video:1",
            target={"root_id": rid, "page": page}))

    monkeypatch.setattr(collector, "fetch_replies", unavailable)
    with httpx.Client() as client:
        _, meta = collector.collect("url", tmp_path / "base", client)
        assert meta["_refresh"]["full_scan_incomplete"] is False
        monkeypatch.setattr(collector, "fetch_main", lambda *_: {
            "replies": [], "cursor": {"is_end": True, "all_count": 0}})
        _, meta = next_collection(tmp_path / "base", tmp_path / "absent", client)
        assert calls == ["100"]
        assert meta["_refresh"]["mode"] == "incremental"
        calls = install_source(monkeypatch, ())
        _, meta = next_collection(tmp_path / "absent", tmp_path / "back", client)
    assert calls == [("100", 1)]
    assert meta["_threads"]["100"]["reply_check_state"] == "complete"
    assert "unavailable" not in meta["_threads"]["100"]


def test_identity_conflict_does_not_overwrite_baseline(monkeypatch, tmp_path):
    install_source(monkeypatch)
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        conflicting = raw(100, count=1)
        conflicting["member"]["mid"] = 2
        monkeypatch.setattr(collector, "fetch_main", lambda *_: {
            "replies": [conflicting], "cursor": {"is_end": True, "all_count": 2}})
        rows, meta = next_collection(tmp_path / "base", tmp_path / "next", client)
    assert {row["author"]["uid"] for row in rows} == {"1"}
    assert "identity_conflict" in meta["coverage"]["reasons"]


def test_recovery_skips_intentionally_reused_floor():
    from app.comment_export.recovery import _next_target

    meta = {"source": {"aid": "1"}, "_threads": {
        "100": {"pagination_status": "partial", "skip_refresh": True},
        "200": {"pagination_status": "partial", "page": 2},
    }}
    assert _next_target({"main_done": True}, meta) == {
        "phase": "replies", "root_id": "200", "page": 2}


def test_resume_does_not_reread_completed_floor_with_retained_gap(monkeypatch, tmp_path):
    from app.comment_export.checkpoint import read_checkpoint
    from app.comment_export.recovery import _next_target

    install_source(monkeypatch, (101, 102))
    roots = [raw(100, count=2), raw(200)]
    monkeypatch.setattr(collector, "fetch_main", lambda *_: {
        "replies": roots, "cursor": {"is_end": True, "all_count": 4}})
    monkeypatch.setattr(collector, "fetch_replies", lambda source, root, page, client: {
        "root": roots[0 if root == "100" else 1],
        "page": {"num": page, "size": 20, "count": 2 if root == "100" else 0},
        "replies": [raw(101, 100), raw(102, 100)] if root == "100" else []})
    with httpx.Client() as client:
        collector.collect("url", tmp_path / "base", client)
        roots[0] = raw(100, count=1)
        calls = []
        fail = True

        def replies(source, root, page, client):
            calls.append((root, page))
            if root == "200" and fail:
                raise collector.CollectionStopped("network_error")
            return {"root": roots[0 if root == "100" else 1],
                    "page": {"num": page, "size": 20, "count": 1 if root == "100" else 0},
                    "replies": [raw(101, 100)] if root == "100" else []}

        monkeypatch.setattr(collector, "fetch_replies", replies)
        next_collection(tmp_path / "base", tmp_path / "next", client, "full")
        _, meta, progress = read_checkpoint(tmp_path / "next")
        assert meta["_threads"]["100"]["pagination_status"] == "partial"
        assert _next_target(progress, meta)["root_id"] == "200"
        fail = False
        collector.collect("url", tmp_path / "next", client, 100, resume=True)
    assert calls == [("100", 1), ("200", 1), ("200", 1)]


def test_refresh_resumes_failure_before_video_resolution(monkeypatch, tmp_path):
    import pytest

    from app.comment_export.refresh import refresh

    install_source(monkeypatch, ())
    resolver = collector.resolve_video
    monkeypatch.setattr(collector, "resolve_video", lambda *_: (_ for _ in ()).throw(
        collector.CollectionStopped("network_error")))
    with httpx.Client() as client:
        with pytest.raises(collector.CollectionStopped, match="network_error"):
            refresh("https://www.bilibili.com/video/BVfake", tmp_path / "task", client,
                    max_requests=100)
        monkeypatch.setattr(collector, "resolve_video", resolver)
        rows, meta = refresh("https://www.bilibili.com/video/BVfake", tmp_path / "task", client,
                             max_requests=999, resume=True)
    assert len(rows) == 1
    assert meta["_refresh"]["requested_mode"] == "auto"
