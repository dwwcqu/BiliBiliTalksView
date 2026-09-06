from app.storage.mapping import comment_values, record_from_row


def test_database_mapping_preserves_extensions_and_null(frozen_case):
    records, manifest = frozen_case
    record = records[0]
    record["content"]["text"] = "a\0b"
    record["extra"] = {"x": "字面\\0"}
    record["author"]["extension"] = "测试"
    state = {
        "state_id": "12345678-1234-4234-8234-123456789012",
        "video_id": manifest["video_id"],
        "source_export_id": manifest["export_id"],
        "schema_version": manifest["schema_version"],
    }
    mapped = comment_values(record, state["state_id"])
    assert record_from_row(mapped, state) == record
