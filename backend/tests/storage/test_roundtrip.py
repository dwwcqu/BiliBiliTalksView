"""Database export preserves protocol values and creates a fresh batch identity."""

from sqlalchemy import insert, update

from app.comment_export.export import build_batch, write_json
from app.comment_export.validation import read_json, read_lines, validate_batch
from app.storage.codec import encode_json
from app.storage.exporter import export_state
from app.storage.mapping import date_value, member_values
from app.storage.payloads import resolve_payloads
from app.storage.schema import comments, threads, videos


def test_database_roundtrip(conn, state_factory, frozen_case, tmp_path):
    records, metadata = frozen_case
    records[0]["content"]["text"] = "NUL\0字面\\0"
    original = build_batch(records, metadata, tmp_path / "source")
    manifest = validate_batch(original)
    manifest["threads"][0]["index_extension"] = {"note": "楼索引扩展"}
    manifest["users"][0]["index_extension"] = {"note": "用户索引扩展"}
    write_json(original / "manifest.json", manifest)
    info = read_json(original / manifest["threads"][0]["path"], "thread")
    info["source_title"] = "来源楼名"
    write_json(original / manifest["threads"][0]["path"], info)
    users = [read_json(original / entry["path"], "user") for entry in manifest["users"]]
    users[0]["extension"] = {"text": "额外\0值"}
    info["extension"] = ["楼扩展"]
    write_json(original / manifest["threads"][0]["path"], info)
    state = state_factory(
        conn,
        video_id=manifest["video_id"],
        lifecycle="ready",
        source_export_id=manifest["export_id"],
        source_metadata=encode_json({"manifest": manifest, "users": users}),
        coverage=encode_json(manifest["coverage"]),
        captured_from=date_value(manifest["captured_from"]),
        captured_to=date_value(manifest["captured_to"]),
    )
    for entry in manifest["threads"]:
        thread = read_json(original / entry["path"], "thread")
        conn.execute(
            insert(threads).values(
                state_id=state["state_id"],
                root_id=entry["root_id"],
                comment_count=thread["comment_count"],
                reply_count=thread["reply_count"],
                participant_count=thread["participant_count"],
                unknown_author_comment_count=thread["unknown_author_comment_count"],
                coverage=encode_json(thread["coverage"]),
                source_metadata=encode_json(thread),
            )
        )
    payload_ids = resolve_payloads(conn, state["video_id"], records)
    conn.execute(insert(comments), [
        member_values(row, state["state_id"], payload_ids[row["comment_id"]]) for row in records
    ])
    conn.execute(
        update(videos)
        .where(videos.c.video_id == manifest["video_id"])
        .values(working_state_id=state["state_id"])
    )
    conn.commit()
    result = export_state(conn, manifest["video_id"], tmp_path / "output", state["state_id"])
    exported = validate_batch(result)
    assert exported["schema_version"] == "2.0.0"
    assert exported["export_id"] != manifest["export_id"]
    assert exported["coverage"] == manifest["coverage"]
    assert exported["counts"] == manifest["counts"]
    assert exported["threads"][0]["index_extension"] == manifest["threads"][0]["index_extension"]
    assert exported["users"][0]["index_extension"] == manifest["users"][0]["index_extension"]
    first_user = read_json(result / exported["users"][0]["path"], "user")
    assert first_user["extension"] == users[0]["extension"]
    actual = []
    for entry in exported["threads"]:
        thread = read_json(result / entry["path"], "thread")
        actual.extend(read_lines(result / thread["comments_path"]))
        if entry["root_id"] == info["root_id"]:
            assert thread["source_title"] == "来源楼名"
            assert thread["extension"] == ["楼扩展"]
    expected = {row["comment_id"]: row for row in records}
    for row in actual:
        assert row["schema_version"] == "2.0.0"
        row["export_id"] = manifest["export_id"]
        row["schema_version"] = manifest["schema_version"]
        assert row == expected[row["comment_id"]]
