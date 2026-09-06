import math
from copy import deepcopy

import pytest

from app.comment_export import refresh as refresh_module
from app.comment_export.checkpoint import Checkpoint, read_checkpoint
from app.comment_export.contract import ContractError
from app.comment_export.export import build_batch


def _saved_work(path, rows, metadata, progress=None):
    cp = Checkpoint(path / "work.sqlite3")
    cp.commit_page("fixture", rows, {
        **(progress or {}), "metadata": metadata,
    })
    cp.close()


def test_read_checkpoint_is_readonly_and_returns_one_snapshot(tmp_path, frozen_case):
    rows, metadata = frozen_case
    task = tmp_path / "task"
    _saved_work(task, rows, metadata, {"main_done": True})
    before = (task / "work.sqlite3").stat().st_mtime_ns

    actual_rows, actual_metadata, actual_progress = read_checkpoint(task)

    assert actual_rows == rows
    assert actual_metadata == metadata
    assert actual_progress["main_done"] is True
    assert (task / "work.sqlite3").stat().st_mtime_ns == before


def test_read_checkpoint_missing_does_not_create_path(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ContractError, match="task_not_found"):
        read_checkpoint(missing)
    assert not missing.exists()


def test_refresh_freezes_work_baseline_before_collecting(tmp_path, frozen_case, monkeypatch):
    rows, metadata = frozen_case
    metadata["_refresh"] = {"version": 1, "trusted": True}
    baseline = tmp_path / "baseline"
    target = tmp_path / "target"
    _saved_work(baseline, rows, metadata, {"main_done": True})
    prepared_metadata = deepcopy(metadata)
    prepared_metadata["export_id"] = "f2da8e84-3138-424f-ab2a-80ca15096a36"
    prepared_progress = {"requests": 0, "finished": False, "frozen_mode": "incremental"}

    def prepare(actual_rows, actual_metadata, actual_progress, mode, observed, interval):
        assert actual_rows == rows
        assert actual_metadata == metadata
        assert actual_progress["main_done"] is True
        assert (mode, observed, interval) == ("auto", "2026-09-06T01:02:03Z", 24)
        return actual_rows, prepared_metadata, prepared_progress

    def collect(url, work_dir, client, max_requests, resume):
        frozen_rows, frozen_metadata, progress = read_checkpoint(work_dir)
        assert frozen_rows == rows
        assert frozen_metadata == prepared_metadata
        assert progress["metadata"] == prepared_metadata
        assert progress["max_requests"] == 77
        assert progress["input_url"] == url
        assert progress["frozen_mode"] == "incremental"
        assert resume is True
        return frozen_rows, frozen_metadata

    monkeypatch.setattr(refresh_module.collector, "now", lambda: "2026-09-06T01:02:03Z")
    monkeypatch.setattr(refresh_module, "prepare_refresh", prepare)
    monkeypatch.setattr(refresh_module.collector, "collect", collect)

    result = refresh_module.refresh(
        "https://www.bilibili.com/video/BVfake", target, object(),
        max_requests=77, baseline_work=baseline,
    )
    assert result == (rows, prepared_metadata)


def test_refresh_resume_uses_frozen_arguments_after_baseline_is_deleted(
        tmp_path, frozen_case, monkeypatch):
    rows, metadata = frozen_case
    target = tmp_path / "target"
    metadata["_refresh"] = {"requested_mode": "auto", "mode": "incremental"}
    _saved_work(target, rows, metadata, {
        "metadata": metadata, "max_requests": 19, "input_url": "https://frozen.invalid",
    })
    calls = []
    monkeypatch.setattr(refresh_module.collector, "collect", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or (rows, metadata))

    assert refresh_module.refresh("https://ignored.invalid", target, object(),
                                  max_requests=999, resume=True, mode="full") == (rows, metadata)
    assert calls[0][0][0] == "https://frozen.invalid"
    assert calls[0][0][3] == 19
    assert calls[0][1] == {"resume": True}


@pytest.mark.parametrize("kwargs,code", [
    ({"baseline_work": "a", "baseline_batch": "b"}, "baseline_conflict"),
    ({"mode": "quick"}, "invalid_refresh_mode"),
    ({"full_interval_hours": 0}, "invalid_full_interval"),
    ({"full_interval_hours": math.inf}, "invalid_full_interval"),
    ({"max_requests": 0}, "invalid_max_requests"),
    ({"resume": True, "baseline_work": "a"}, "resume_with_baseline"),
])
def test_refresh_rejects_invalid_arguments_before_collection(tmp_path, monkeypatch, kwargs, code):
    monkeypatch.setattr(refresh_module.collector, "collect",
                        lambda *args, **kw: pytest.fail("source collection was reached"))
    with pytest.raises(ContractError, match=code):
        refresh_module.refresh("https://example.invalid", tmp_path / "target", object(), **kwargs)


def test_refresh_rejects_existing_target_and_same_baseline_before_collection(
        tmp_path, frozen_case, monkeypatch):
    rows, metadata = frozen_case
    target = tmp_path / "target"
    _saved_work(target, rows, metadata)
    monkeypatch.setattr(refresh_module.collector, "collect",
                        lambda *args, **kw: pytest.fail("source collection was reached"))

    with pytest.raises(ContractError, match="checkpoint_exists_use_resume"):
        refresh_module.refresh("https://example.invalid", target, object())
    with pytest.raises(ContractError, match="baseline_is_target"):
        refresh_module.refresh("https://example.invalid", target, object(),
                               baseline_work=target)


def test_refresh_rejects_source_identity_mismatch_recorded_by_collector(
        tmp_path, frozen_case, monkeypatch):
    rows, metadata = frozen_case
    baseline = tmp_path / "baseline"
    target = tmp_path / "target"
    _saved_work(baseline, rows, metadata)

    def collect(url, work_dir, client, max_requests, resume):
        cp = Checkpoint(work_dir / "work.sqlite3")
        try:
            progress = cp.get_progress()
            progress["stopped_reason"] = "checkpoint_video_mismatch"
            cp.set_progress(progress)
        finally:
            cp.close()
        return read_checkpoint(work_dir)[:2]

    monkeypatch.setattr(refresh_module.collector, "collect", collect)

    with pytest.raises(ContractError, match="checkpoint_video_mismatch"):
        refresh_module.refresh("https://example.invalid", target, object(),
                               baseline_work=baseline)


def test_invalid_work_baseline_is_reported_safely_before_target_creation(
        tmp_path, frozen_case, monkeypatch):
    rows, metadata = frozen_case
    metadata.pop("captured_to")
    baseline = tmp_path / "baseline"
    target = tmp_path / "target"
    _saved_work(baseline, rows, metadata)
    monkeypatch.setattr(refresh_module.collector, "collect",
                        lambda *args, **kw: pytest.fail("source collection was reached"))

    with pytest.raises(ContractError, match="invalid_baseline"):
        refresh_module.refresh("https://example.invalid", target, object(),
                               baseline_work=baseline)
    assert not (target / "work.sqlite3").exists()


def test_file_baseline_rebuilds_thread_coverage_and_strips_internal_extensions(
        tmp_path, frozen_case, monkeypatch):
    rows, metadata = frozen_case
    rows = [dict(row, schema_version="2.0.0") for row in rows]
    metadata = dict(metadata, schema_version="2.0.0", _refresh={"untrusted": True},
                    _vendor={"keep": False}, public_extension={"keep": True})
    for row in rows:
        row["export_id"] = metadata["export_id"]
    batch = build_batch(rows, metadata, tmp_path / "batch")
    from app.comment_export.export import write_json
    from app.comment_export.validation import read_json
    manifest = read_json(batch / "manifest.json", "manifest")
    manifest["_refresh"] = {"mode": "incremental", "last_full_scan_completed_at": "2099-01-01T00:00:00Z"}
    write_json(batch / "manifest.json", manifest)
    captured = {}

    def prepare(actual_rows, actual_metadata, progress, *args):
        captured.update(rows=actual_rows, metadata=actual_metadata, progress=progress)
        return actual_rows, actual_metadata, progress

    monkeypatch.setattr(refresh_module, "prepare_refresh", prepare)
    monkeypatch.setattr(refresh_module.collector, "collect",
                        lambda *args, **kwargs: read_checkpoint(args[1])[:2])

    refresh_module.refresh("https://example.invalid", tmp_path / "target", object(),
                           baseline_batch=batch)

    assert {row["comment_id"] for row in captured["rows"]} == {"100", "101", "200"}
    assert captured["metadata"]["public_extension"] == {"keep": True}
    assert "_refresh" not in captured["metadata"]
    assert captured["metadata"]["_vendor"] == {"keep": False}
    assert captured["metadata"]["_threads"] == {
        "100": {"pagination_status": "verified", "count": 1},
        "200": {"pagination_status": "verified", "count": 0},
    }
