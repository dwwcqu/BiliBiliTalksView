from app.comment_export.normalization import normalize_comment


def test_large_ids_and_parent_preserved():
    raw = {"rpid_str": "12345678901234567890", "root_str": "100", "parent_str": "101",
           "member": {"mid": "33", "uname": "测试"}, "content": {"message": "甲\n乙"},
           "ctime": 1, "like": 2}
    row = normalize_comment(raw, "100", "bilibili:video:1", "94a08de2-a412-4b56-b0aa-7f606b854fc7",
                            "2026-09-05T09:00:00Z")
    assert row["comment_id"] == "12345678901234567890"
    assert row["parent_id"] == "101"
    assert row["author"] == {"uid": "33", "nickname": "测试"}
    assert row["content"]["text"] == "甲\n乙"


def test_root_and_unknown_author():
    row = normalize_comment({"rpid": 100, "root": 0, "content": {}}, None,
                            "bilibili:video:1", "94a08de2-a412-4b56-b0aa-7f606b854fc7",
                            "2026-09-05T09:00:00Z")
    assert row["root_id"] == "100"
    assert row["author"]["uid"] is None
    assert row["parent_id"] is None
    assert row["content"]["text"] is None
