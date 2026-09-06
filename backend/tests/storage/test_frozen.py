import json

from app.comment_export.export import build_batch
from app.storage.frozen import freeze_batch


def test_freeze_is_independent_and_format_insensitive(frozen_case, tmp_path):
    source = build_batch(*frozen_case, tmp_path / "source")
    with freeze_batch(source, tmp_path / "work") as first:
        digest = first.digest
        original = (first.directory / "manifest.json").read_bytes()
        path = source / "manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        assert (first.directory / "manifest.json").read_bytes() == original
    assert not first.directory.exists()
    with freeze_batch(source, tmp_path / "work") as second:
        assert second.digest == digest


def test_nonempty_readme_rejected_without_changing_source(frozen_case, tmp_path):
    import pytest

    from app.storage.errors import StorageError

    source = build_batch(*frozen_case, tmp_path / "source")
    readme = source / "README.md"
    readme.write_text("下游说明", encoding="utf-8")
    with pytest.raises(StorageError, match="invalid_export"), freeze_batch(source, tmp_path / "work"):
        pass
    assert readme.read_text(encoding="utf-8") == "下游说明"
