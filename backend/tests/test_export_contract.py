"""Contract boundaries use only invented user data."""
from copy import deepcopy

import pytest

from app.comment_export.contract import ContractError, parse_json, validate_record


@pytest.fixture
def comment():
    return {
        "schema_version": "1.0.0", "export_id": "94a08de2-a412-4b56-b0aa-7f606b854fc7",
        "video_id": "bilibili:video:10001", "comment_id": "90001", "root_id": "90001",
        "parent_id": None, "kind": "root", "author": {"uid": "123", "nickname": "虚构用户"},
        "reply_relation": {"status": "not_applicable", "target_uid": None},
        "content": {"text": "第一行\n第二行", "images": [], "emotes": []},
        "created_at": "2026-09-05T08:00:00Z", "collected_at": "2026-09-05T09:00:00Z",
        "like_count": 0,
    }


def test_accepts_large_string_identity(comment):
    comment["author"]["uid"] = "12345678901234567890"
    validate_record("comment", comment)


@pytest.mark.parametrize("change", [
    {"schema_version": "3.0.0"}, {"created_at": "2026-02-30T08:00:00Z"},
    {"comment_id": 90001}, {"like_count": -1}, {"root_id": "90002"},
    {"parent_id": "90001"}, {"export_id": "not-a-uuid"},
])
def test_rejects_invalid_comment(comment, change):
    comment.update(change)
    with pytest.raises(ContractError):
        validate_record("comment", comment)


def test_rejects_numeric_uid(comment):
    comment["author"]["uid"] = 123
    with pytest.raises(ContractError):
        validate_record("comment", comment)


def test_required_nullable_field_cannot_be_omitted(comment):
    del comment["parent_id"]
    with pytest.raises(ContractError):
        validate_record("comment", comment)


def test_unknown_author_preserved(comment):
    comment["author"] = {"uid": None, "nickname": None}
    validate_record("comment", comment)


def test_unknown_reply_relation(comment):
    reply = deepcopy(comment)
    reply.update(comment_id="90002", kind="reply")
    reply["reply_relation"]["status"] = "unknown"
    validate_record("comment", reply)
    reply["reply_relation"]["status"] = "source"
    with pytest.raises(ContractError):
        validate_record("comment", reply)


@pytest.mark.parametrize("text", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_rejects_ambiguous_json(text):
    with pytest.raises(ContractError):
        parse_json(text)
