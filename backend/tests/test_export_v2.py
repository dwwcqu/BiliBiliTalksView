import json
import re
from copy import deepcopy

import pytest

from app.comment_export.contract import ContractError, validate_record
from app.comment_export.export import build_batch
from app.comment_export.validation import validate_batch


def v2_case(frozen_case):
    records, manifest = deepcopy(frozen_case)
    manifest["schema_version"] = "2.0.0"
    for row in records:
        row["schema_version"] = "2.0.0"
        row["author"]["nickname"] = "中文 名称\n另行\t符号"
    return records, manifest


def test_v2_paths_ascii_and_complete_thread(frozen_case, tmp_path):
    records, manifest = v2_case(frozen_case)
    batch = build_batch(records, manifest, tmp_path / "batch")
    result = validate_batch(batch)
    assert result["schema_version"] == "2.0.0"
    for path in batch.rglob("*"):
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", path.name)
    assert (batch / "users/uid_1/comments.jsonl").exists()
    assert (batch / "users/unknown/comments.jsonl").exists()
    thread = [json.loads(line) for line in (batch / "threads/000/comments.jsonl").read_text(encoding="utf-8").split("\n") if line]
    assert [row["comment_id"] for row in thread] == ["100", "101"]
    assert thread[0]["author"]["nickname"] == "中文 名称\n另行\t符号"


def test_v1_reader_still_supported(frozen_case, tmp_path):
    batch = build_batch(*frozen_case, tmp_path / "legacy")
    assert validate_batch(batch)["schema_version"] == "1.0.0"


@pytest.mark.parametrize("path", ["用户评论目录/x/user.json", "users/uid 1/user.json", "users/uid_1\n/user.json"])
def test_v2_schema_rejects_nonportable_paths(frozen_case, path):
    _, meta = v2_case(frozen_case)
    pointer = {k:meta[k] for k in ("schema_version", "export_id", "video_id")}
    pointer["batch_path"] = path
    with pytest.raises(ContractError):
        validate_record("current", pointer)


@pytest.mark.parametrize("bad_path", ["users/uid 1/user.json", "users/uid_1/user.json\n", "users/uid_1/user.json\t"])
def test_v2_manifest_rejects_whitespace_in_index_path(frozen_case, tmp_path, bad_path):
    batch = build_batch(*v2_case(frozen_case), tmp_path / "batch")
    manifest = validate_batch(batch)
    manifest["users"][0]["path"] = bad_path
    with pytest.raises(ContractError):
        validate_record("manifest", manifest)
