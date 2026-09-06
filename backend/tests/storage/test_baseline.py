from copy import deepcopy
from uuid import uuid4

from app.comment_export.checkpoint import read_checkpoint
from app.comment_export.export import build_batch
from app.comment_export.incremental import prepare_refresh
from app.storage.frozen import freeze_batch
from app.storage.importer import import_batch
from app.storage.queries import read_state


def test_database_baseline_uses_latest_partial_without_changing_current(
    conn, frozen_case, tmp_path
):
    from app.storage.baseline import freeze_baseline

    rows, meta = deepcopy(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "one"), tmp_path / "freeze") as batch:
        current = import_batch(conn, batch, publish=True)
    meta.update(
        export_id=str(uuid4()),
        captured_to="2026-09-05T09:02:00Z",
        exported_at="2026-09-05T09:03:00Z",
    )
    meta["coverage"].update(
        status="partial", replies_pagination="partial", reasons=["replies_incomplete"]
    )
    meta["_threads"]["100"]["pagination_status"] = "partial"
    for row in rows:
        row["export_id"] = meta["export_id"]
    with freeze_batch(build_batch(rows, meta, tmp_path / "two"), tmp_path / "freeze") as batch:
        partial = import_batch(conn, batch)
    info = freeze_baseline(conn, meta["video_id"], tmp_path / "baseline")
    assert info["state_id"] == partial["state_id"]
    assert read_state(conn, meta["video_id"])["state_id"] == current["state_id"]
    saved, metadata, progress = read_checkpoint(tmp_path / "baseline")
    assert len(saved) == 3
    assert "_refresh" not in metadata
    _, fresh, _ = prepare_refresh(saved, metadata, progress, "auto", "2026-09-05T10:00:00Z", 24)
    assert fresh["_refresh"]["mode"] == "full"


def test_empty_database_baseline_creates_no_checkpoint(conn, tmp_path):
    from app.storage.baseline import freeze_baseline

    target = tmp_path / "empty"
    result = freeze_baseline(conn, "bilibili:video:1", target)
    assert result["state_id"] is None
    assert result["cache_version"] == 0
    assert not (target / "work.sqlite3").exists()


def test_failed_sqlite_freeze_leaves_target_retryable(conn, frozen_case, tmp_path, monkeypatch):
    import pytest

    from app.comment_export.checkpoint import Checkpoint
    from app.storage.baseline import freeze_baseline

    rows, meta = frozen_case
    with freeze_batch(build_batch(rows, meta, tmp_path / "batch"), tmp_path / "freeze") as batch:
        import_batch(conn, batch, publish=True)
    original = Checkpoint.commit_page

    def fail(*args, **kwargs):
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(Checkpoint, "commit_page", fail)
    target = tmp_path / "baseline"
    with pytest.raises(OSError, match="simulated disk"):
        freeze_baseline(conn, meta["video_id"], target)
    assert not (target / "work.sqlite3").exists()
    monkeypatch.setattr(Checkpoint, "commit_page", original)
    assert freeze_baseline(conn, meta["video_id"], target)["state_id"]
    assert len(read_checkpoint(target)[0]) == 3
