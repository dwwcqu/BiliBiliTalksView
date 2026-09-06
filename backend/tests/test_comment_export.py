import json
from copy import deepcopy

import pytest

from app.comment_export.contract import ContractError
from app.comment_export.export import build_batch
from app.comment_export.validation import validate_batch


def test_projection_counts_and_gaps(frozen_case, tmp_path):
    records, metadata = frozen_case
    batch = build_batch(records, metadata, tmp_path / "batch")
    manifest = validate_batch(batch)
    assert manifest["counts"] == {"root_comments": 2, "replies": 1, "comments": 3,
                                  "known_users": 1, "unknown_author_comments": 1,
                                  "unclassified_records": 0}
    assert manifest["coverage"]["status"] == "verified"
    assert manifest["coverage"]["context_status"] == "gaps"
    user = json.loads((batch / "用户评论目录/1_甲_乙/user.json").read_text(encoding="utf-8"))
    assert user["thread_ids"] == ["100", "200"]
    assert user["context_status"] == "gaps"
    assert user["reasons"] == manifest["coverage"]["reasons"]
    assert (batch / "README.md").read_bytes() == b""
    lines = (batch / "楼内对话目录/000/comments.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["comment_id"] for x in lines] == ["100", "101"]


def test_detects_changed_user_copy(frozen_case, tmp_path):
    batch = build_batch(*frozen_case, tmp_path / "batch")
    path = batch / "用户评论目录/1_甲_乙/comments.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["content"]["text"] = "被篡改"
    path.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(ContractError):
        validate_batch(batch)


def test_conflicting_duplicate_not_silently_overwritten(frozen_case, tmp_path):
    records, meta = frozen_case
    duplicate = deepcopy(records[0])
    duplicate["author"]["uid"] = "42"
    with pytest.raises(ContractError):
        build_batch(records + [duplicate], meta, tmp_path / "batch")


def test_unicode_separators_are_content_not_jsonl_lines(frozen_case, tmp_path):
    records, metadata = frozen_case
    records[0]["content"]["text"] = "甲\u0085乙\u2028丙\u2029丁"
    batch = build_batch(records, metadata, tmp_path / "batch")
    assert validate_batch(batch)["counts"]["comments"] == 3


def test_cannot_hide_known_context_gaps(frozen_case, tmp_path):
    batch = build_batch(*frozen_case, tmp_path / "batch")
    for path in batch.rglob("*.json"):
        obj = json.loads(path.read_text(encoding="utf-8"))
        if "coverage" in obj:
            obj["coverage"]["context_status"] = "no_known_gaps"
            obj["coverage"]["reasons"] = []
        if "context_status" in obj:
            obj["context_status"] = "no_known_gaps"
            obj["reasons"] = []
        path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ContractError):
        validate_batch(batch)


def test_unpaired_surrogate_is_escaped_on_disk(frozen_case, tmp_path):
    records, meta = frozen_case
    records[0]["content"]["text"] = "代理\ud800字符"
    records[0]["author"]["nickname"] = "昵称\udfff"
    batch = build_batch(records, meta, tmp_path / "batch")
    assert validate_batch(batch)["counts"]["comments"] == 3
