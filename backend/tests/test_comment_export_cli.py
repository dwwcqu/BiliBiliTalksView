import pytest

from app.comment_export.cli import main


def test_collect_requires_explicit_url():
    with pytest.raises(SystemExit) as error:
        main(["collect"])
    assert error.value.code == 2


def test_validate_missing_batch_returns_two(tmp_path):
    assert main(["validate", "--batch", str(tmp_path)]) == 2


def test_collect_uses_requested_output_root(frozen_case, tmp_path, monkeypatch):
    import httpx

    from app.comment_export import cli
    from app.comment_export.checkpoint import Checkpoint
    from app.comment_export.publication import read_current

    records, metadata = frozen_case
    def fake_collect(url, work_dir, client, max_requests, resume):
        cp = Checkpoint(work_dir / "work.sqlite3")
        try:
            cp.initialize(metadata)
            cp.commit_page("fixture", records, {})
        finally:
            cp.close()
        return records, metadata

    monkeypatch.setattr(cli, "collect", fake_collect)
    monkeypatch.setattr(cli, "_client", httpx.Client)
    output = tmp_path / "custom output"
    result = main(["collect", "--url", "https://www.bilibili.com/video/BV1234567890",
                   "--work-dir", str(tmp_path / "work"), "--output", str(output)])
    assert result == 0
    manifest, rows = read_current(output / "bilibili-video-10001")
    assert len(rows) == 3
    assert manifest["video_id"] == "bilibili:video:10001"


def test_diagnose_does_not_load_cookie_or_create_control(tmp_path, monkeypatch, capsys):
    from app.comment_export.checkpoint import Checkpoint
    task = tmp_path / "task"
    cp = Checkpoint(task / "work.sqlite3")
    cp.set_progress({"blocked": True, "stopped_reason": "access_restricted"})
    cp.close()
    monkeypatch.setenv("BILIBILI_COOKIE_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("BILIBILI_CONTROL_DIR", str(tmp_path / "control"))
    assert main(["diagnose", "--work-dir", str(task)]) == 0
    assert "unknown_legacy" in capsys.readouterr().out
    assert not (tmp_path / "control").exists()


def test_refresh_cli_exports_complete_snapshot_then_reuses_floor(tmp_path, monkeypatch, capsys):
    import json

    import httpx

    from app.comment_export import cli, collector
    from app.comment_export.publication import read_current

    root = {"rpid": 100, "root": 0, "parent": 0, "member": {"mid": 1, "uname": "user"},
            "content": {"message": "root"}, "ctime": 1, "like": 0, "rcount": 1}
    reply = {**root, "rpid": 101, "root": 100, "parent": 100, "rcount": 0}
    monkeypatch.setattr(cli, "_client", httpx.Client)
    monkeypatch.setattr(collector, "resolve_video", lambda *_: {
        "source": {"platform": "bilibili", "aid": "1", "oid": "1", "bvid": "BVfake",
                   "episode_id": None, "comment_type": 1},
        "canonical_url": "https://www.bilibili.com/video/BVfake", "title": "sample"})
    monkeypatch.setattr(collector, "get_signing_keys", lambda *_: ("a", "b"))
    monkeypatch.setattr(collector, "fetch_main", lambda *_: {
        "replies": [root], "cursor": {"is_end": True, "all_count": 2}})
    calls = []

    def replies(*_):
        calls.append(1)
        return {"root": root, "replies": [reply], "page": {"num": 1, "size": 20, "count": 1}}

    monkeypatch.setattr(collector, "fetch_replies", replies)
    output = tmp_path / "output"
    common = ["--url", "https://www.bilibili.com/video/BVfake", "--output", str(output)]
    assert main(["refresh", *common, "--work-dir", str(tmp_path / "first")]) == 0
    capsys.readouterr()
    assert main(["refresh", *common, "--work-dir", str(tmp_path / "next"),
                 "--baseline-work", str(tmp_path / "first")]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "incremental"
    assert result["counts"]["comments"] == 2
    assert calls == [1]
    assert result["stats"]["reused_unverified_comments"] == 1
    current, _ = read_current(output / "bilibili-video-1")
    assert current["coverage"]["status"] == "verified"
    from pathlib import Path

    from app.comment_export.validation import validate_batch
    assert validate_batch(Path(result["path"]))["coverage"]["status"] == "partial"
    assert main(["refresh", *common, "--work-dir", str(tmp_path / "next"), "--resume"]) == 3
    assert calls == [1]
