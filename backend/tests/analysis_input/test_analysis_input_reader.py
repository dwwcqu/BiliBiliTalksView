import json
import shutil

import pytest

from app.analysis_input.errors import InputPreparationError
from app.analysis_input.reader import stage_current
from app.storage import frozen


@pytest.mark.parametrize("version", ["1.0.0", "2.0.0"])
def test_stage_fixed_input(make_export, tmp_path, version):
    source, batch = make_export(version=version)
    target = tmp_path / "staged"
    result = stage_current(source, target)
    assert result["schema_version"] == version
    shutil.rmtree(batch)
    assert (target / "README.md").read_text(encoding="utf-8") == "合成视频背景"


def test_no_pointer(tmp_path):
    with pytest.raises(InputPreparationError, match="not_published"):
        stage_current(tmp_path / "missing", tmp_path / "target")


def test_corrupt_same_batch(make_export, tmp_path):
    source, batch = make_export()
    (batch / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(InputPreparationError, match="invalid_export"):
        stage_current(source, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_failed_read_restarts_whole_batch(make_export, tmp_path, monkeypatch):
    source, old = make_export()
    real_copy = frozen.shutil.copytree
    calls = []
    def race(src, dst, *args, **kwargs):
        calls.append(str(src))
        if len(calls) == 1:
            make_export(container=source)
            raise FileNotFoundError("old cleaned")
        return real_copy(src, dst, *args, **kwargs)
    monkeypatch.setattr(frozen.shutil, "copytree", race)
    manifest = stage_current(source, tmp_path / "target")
    assert manifest["export_id"] != old.name


def test_successful_old_read_does_not_chase_new(make_export, tmp_path, monkeypatch):
    source, old = make_export()
    real_validate = frozen.load_validated_documents
    def switch_after_copy(path, **kwargs):
        result = real_validate(path, **kwargs)
        make_export(container=source)
        return result
    monkeypatch.setattr(frozen, "load_validated_documents", switch_after_copy)
    assert stage_current(source, tmp_path / "target")["export_id"] == old.name


@pytest.mark.parametrize("mutation,expected", [("missing", "export_unavailable"),
                                               ("invalid", "invalid_export")])
def test_pointer_failure_on_retry(make_export, tmp_path, monkeypatch, mutation, expected):
    source, _ = make_export()
    def fail(*args, **kwargs):
        pointer = source / "current.json"
        if mutation == "missing":
            pointer.unlink()
        else:
            pointer.write_text("{}", encoding="utf-8")
        raise FileNotFoundError("batch missing")
    monkeypatch.setattr(frozen.shutil, "copytree", fail)
    with pytest.raises(InputPreparationError, match=expected):
        stage_current(source, tmp_path / "target")


def test_refresh_retry_bound(make_export, tmp_path, monkeypatch):
    from uuid import uuid4
    source, _ = make_export()
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(1)
        pointer = source / "current.json"
        obj = json.loads(pointer.read_text())
        obj.update(export_id=str(uuid4()))
        obj["batch_path"] = "batches/" + obj["export_id"]
        pointer.write_text(json.dumps(obj), encoding="utf-8")
        raise FileNotFoundError("changed")
    monkeypatch.setattr(frozen.shutil, "copytree", fail)
    with pytest.raises(InputPreparationError, match="batch_changed"):
        stage_current(source, tmp_path / "target")
    assert len(attempts) == 3


def test_current_link_outside_container_rejected(make_export, tmp_path):
    external, _ = make_export()
    container = tmp_path / "linked-source"
    container.mkdir()
    try:
        (container / "current.json").symlink_to(external / "current.json")
    except OSError:
        pytest.skip("OS cannot create symbolic links")
    with pytest.raises(InputPreparationError, match="invalid_export"):
        stage_current(container, tmp_path / "target")


def test_invalid_metadata_encoding_after_refresh_restarts(make_export, tmp_path, monkeypatch):
    source, old = make_export()
    manifest = old / "manifest.json"
    manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
    real_copy = frozen.shutil.copytree
    changed = False
    def race(src, dst, *args, **kwargs):
        nonlocal changed
        result = real_copy(src, dst, *args, **kwargs)
        if not changed:
            changed = True
            make_export(container=source)
        return result
    monkeypatch.setattr(frozen.shutil, "copytree", race)
    assert stage_current(source, tmp_path / "target")["export_id"] != old.name


def test_current_link_detected_before_resolve(make_export, tmp_path, monkeypatch):
    from pathlib import Path
    source, _ = make_export()
    pointer = source / "current.json"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == pointer or original(self))
    with pytest.raises(InputPreparationError, match="invalid_export"):
        stage_current(source, tmp_path / "target")


def test_source_junction_rejected_before_copy(make_export, tmp_path, monkeypatch):
    from pathlib import Path
    source, batch = make_export()
    suspect = batch / "linked"
    suspect.mkdir()
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == suspect or original(self))
    called = []
    actual_copy = frozen.shutil.copytree
    def copy(*args, **kwargs):
        called.append(True)
        return actual_copy(*args, **kwargs)
    monkeypatch.setattr(frozen.shutil, "copytree", copy)
    with pytest.raises(InputPreparationError, match="invalid_export"):
        stage_current(source, tmp_path / "target")
    assert not called
