"""Analysis mode must not weaken the original export contract."""
from copy import deepcopy

import pytest

from app.comment_export.contract import ContractError
from app.comment_export.export import build_batch
from app.comment_export.validation import validate_batch
from app.storage.errors import StorageError
from app.storage.frozen import freeze_batch


@pytest.mark.parametrize("version", ["1.0.0", "2.0.0"])
def test_analysis_readme_preserved(frozen_case, tmp_path, version):
    records, metadata = deepcopy(frozen_case)
    metadata["schema_version"] = version
    for row in records:
        row["schema_version"] = version
    source = build_batch(records, metadata, tmp_path / "source")
    background = "虚构背景：用于分析。".encode()
    (source / "README.md").write_bytes(background)
    with pytest.raises(ContractError, match="nonempty_readme"):
        validate_batch(source)
    with pytest.raises(StorageError), freeze_batch(source, tmp_path / "raw-work"):
        pass
    with freeze_batch(source, tmp_path / "analysis-work", analysis_readme=True) as batch:
        assert batch.manifest["schema_version"] == version
        assert (batch.directory / "README.md").read_bytes() == background
    assert not batch.directory.exists()
    assert (source / "README.md").read_bytes() == background


def test_analysis_frozen_dataset_preserves_custom_digest_and_documents(frozen_case, tmp_path):
    source = build_batch(*frozen_case, tmp_path / "source")
    background = "分析背景"
    (source / "README.md").write_text(background, encoding="utf-8")
    calls = []

    def custom_digest(directory):
        calls.append(directory)
        assert (directory / "README.md").read_text(encoding="utf-8") == background
        return "a" * 64

    with freeze_batch(source, tmp_path / "work", analysis_readme=True,
                      digest_function=custom_digest) as batch:
        assert batch.digest == batch.dataset.digest == "a" * 64
        assert batch.read_document("README.md") == background
        assert batch.read_document("manifest.json") == validate_batch(source, analysis_readme=True)
        info = batch.read_document(batch.manifest["threads"][0]["path"])
        rows = batch.read_lines(info["comments_path"])
        rows.clear()
        assert batch.read_lines(info["comments_path"])
    assert len(calls) == 1


def test_exact_digest_with_readme_retains_decimal_projection_checks(frozen_case, tmp_path):
    from decimal import Decimal

    from app.analysis_input.digest import exact_digest
    from app.storage.frozen import canonical_digest

    source = build_batch(*frozen_case, tmp_path / "source")
    (source / "README.md").write_text("Analysis context", encoding="utf-8")
    projections = sorted(source.rglob("comments.jsonl"))
    for path in projections:
        raw = b"\n".join(line.replace(b'{', b'{"precision":1.00000000000000001,', 1)
                         if line else line for line in path.read_bytes().split(b"\n"))
        path.write_bytes(raw)
    with freeze_batch(source, tmp_path / "work", analysis_readme=True,
                      digest_function=exact_digest) as batch:
        first = batch.digest
        assert first == exact_digest(source) == batch.dataset.digest
        assert first != canonical_digest(source)
        assert next(batch.iter_comments())["precision"] == Decimal("1.00000000000000001")
    for path in projections:
        path.write_bytes(path.read_bytes().replace(b'1.00000000000000001',
                                                  b'1.00000000000000002'))
    with freeze_batch(source, tmp_path / "work", analysis_readme=True,
                      digest_function=exact_digest) as batch:
        assert batch.digest != first
    projections[0].write_bytes(projections[0].read_bytes().replace(
        b'1.00000000000000002', b'1.00000000000000001'))
    # Ordinary float decoding cannot distinguish the numbers; analysis must reject them.
    with pytest.raises(StorageError, match="invalid_export"), freeze_batch(
        source, tmp_path / "work", analysis_readme=True, digest_function=exact_digest,
    ):
        pass
