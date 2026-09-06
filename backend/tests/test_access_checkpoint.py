import pytest

from app.comment_export.checkpoint import Checkpoint, read_control_snapshot
from app.comment_export.contract import ContractError


def test_readonly_missing_task_does_not_create_directory(tmp_path):
    task = tmp_path / "missing"
    with pytest.raises(ContractError, match="task_not_found"):
        read_control_snapshot(task)
    assert not task.exists()


def test_revision_changes_only_with_new_committed_page(tmp_path):
    cp = Checkpoint(tmp_path / "work.sqlite3")
    cp.initialize({"video_id": "bilibili:video:1"})
    cp.commit_page("page:1", [], {"requests": 1})
    assert cp.get_progress()["checkpoint_revision"] == 1
    cp.set_progress({"requests": 2, "checkpoint_revision": 0})
    cp.commit_page("page:1", [], {})
    assert cp.get_progress()["checkpoint_revision"] == 1
    cp.commit_page("page:2", [], {"requests": 1})
    assert cp.get_progress()["checkpoint_revision"] == 2
    assert cp.get_progress()["requests"] == 2
    cp.close()
    before = (tmp_path / "work.sqlite3").stat().st_mtime_ns
    snapshot = read_control_snapshot(tmp_path)
    assert snapshot["progress"]["checkpoint_revision"] == 2
    assert (tmp_path / "work.sqlite3").stat().st_mtime_ns == before
