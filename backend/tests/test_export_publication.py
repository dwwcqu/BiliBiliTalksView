from copy import deepcopy

import pytest

from app.comment_export.contract import ContractError
from app.comment_export.export import build_batch
from app.comment_export.publication import publish_batch, read_current


def test_published_batch_readable(frozen_case, tmp_path):
    batch = build_batch(*frozen_case, tmp_path / "work")
    container = tmp_path / "out"
    publish_batch(batch, container)
    manifest, rows = read_current(container)
    assert manifest["counts"]["comments"] == 3
    assert len(rows) == 3


def test_partial_does_not_replace_verified(frozen_case, tmp_path):
    records, metadata = frozen_case
    container = tmp_path / "out"
    publish_batch(build_batch(records, metadata, tmp_path / "first"), container)
    changed = deepcopy(metadata)
    changed["export_id"] = "12345678-1234-4234-8234-123456789012"
    changed["coverage"]["status"] = "partial"
    changed["coverage"]["main_pagination"] = "partial"
    rows = [dict(row, export_id=changed["export_id"]) for row in records]
    second = build_batch(rows, changed, tmp_path / "second")
    with pytest.raises(ContractError, match="partial_not_published"):
        publish_batch(second, container)
    assert read_current(container)[0]["export_id"] == metadata["export_id"]
    assert second.exists()


def test_missing_entry_is_explicit(tmp_path):
    with pytest.raises(ContractError, match="not_published"):
        read_current(tmp_path)


def test_failed_pointer_replace_keeps_old(frozen_case, tmp_path, monkeypatch):
    from app.comment_export import publication
    records, metadata = frozen_case
    container = tmp_path / "out"
    publish_batch(build_batch(records, metadata, tmp_path / "first"), container)
    changed = deepcopy(metadata)
    changed["export_id"] = "12345678-1234-4234-8234-123456789012"
    rows = [dict(row, export_id=changed["export_id"]) for row in records]
    batch = build_batch(rows, changed, tmp_path / "second")
    def fail(*args):
        raise OSError("simulated replacement failure")
    monkeypatch.setattr(publication.os, "replace", fail)
    with pytest.raises(OSError):
        publish_batch(batch, container)
    assert read_current(container)[0]["export_id"] == metadata["export_id"]


def test_malformed_pointer_is_invalid_export(tmp_path):
    (tmp_path / "current.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ContractError, match="^invalid_export$"):
        read_current(tmp_path)


def test_transient_windows_rename_is_retried(frozen_case, tmp_path, monkeypatch):
    from pathlib import Path

    source = build_batch(*frozen_case, tmp_path / "source")
    original = Path.rename
    attempts = []
    def rename(path, target):
        attempts.append(path)
        if len(attempts) == 1:
            error = PermissionError("temporary sharing conflict")
            error.winerror = 5
            raise error
        return original(path, target)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    publish_batch(source, tmp_path / "out")
    assert len(attempts) == 2


def test_unpublished_valid_target_can_be_recovered(frozen_case, tmp_path):
    container = tmp_path / "out"
    target = container / "batches" / frozen_case[1]["export_id"]
    build_batch(*frozen_case, target)
    assert publish_batch(target, container) == target
    assert read_current(container)[0]["counts"]["comments"] == 3
