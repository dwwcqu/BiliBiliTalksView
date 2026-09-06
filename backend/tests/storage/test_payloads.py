from copy import deepcopy

import pytest


def test_payload_hash_ignores_key_order_not_array_order():
    from app.storage.payloads import payload_digest

    left = {"content": {"text": "讨论", "media": [1, 2]}, "extra_fields": {}}
    same = {"extra_fields": {}, "content": {"media": [1, 2], "text": "讨论"}}
    changed = {"content": {"text": "讨论", "media": [2, 1]}, "extra_fields": {}}
    assert payload_digest(left) == payload_digest(same)
    assert payload_digest(left) != payload_digest(changed)


def test_observation_metadata_does_not_change_payload(frozen_case):
    from app.storage.payloads import payload_document

    record = frozen_case[0][0]
    changed = deepcopy(record)
    changed.update(like_count=42, collected_at="2026-09-06T09:00:00Z", export_id="new")
    changed["author"]["nickname"] = "新昵称"
    assert payload_document(record) == payload_document(changed)


@pytest.mark.parametrize("part", ["text", "images", "emotes", "top", "author"])
def test_body_and_unknown_extensions_change_payload(frozen_case, part):
    from app.storage.payloads import payload_document

    record = frozen_case[0][0]
    changed = deepcopy(record)
    if part in {"text", "images", "emotes"}:
        changed["content"][part] = "changed"
    elif part == "top":
        changed["extension"] = {"new": "value"}
    else:
        changed["author"]["extension"] = "value"
    assert payload_document(record) != payload_document(changed)


def test_payload_hash_special_characters_roundtrip(frozen_case):
    from app.storage.codec import decode_json, encode_json
    from app.storage.payloads import payload_digest, payload_document

    record = frozen_case[0][0]
    record["content"]["text"] = "nul\0 literal\\0 surrogate\ud800"
    record["ext\0"] = {"\udfff": "\\uD800"}
    document = payload_document(record)
    restored = decode_json(encode_json(document))
    assert restored == document
    assert payload_digest(restored) == payload_digest(document)


def test_import_reuses_payloads_without_insert(conn, frozen_case, tmp_path):
    from uuid import uuid4

    from sqlalchemy import event, func, select

    from app.comment_export.export import build_batch
    from app.storage.frozen import freeze_batch
    from app.storage.importer import import_batch
    from app.storage.queries import list_thread_comments
    from app.storage.schema import comment_payloads, comments

    rows, meta = deepcopy(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "first"), tmp_path / "work") as batch:
        first = import_batch(conn, batch, publish=True)
    meta.update(export_id=str(uuid4()), captured_to="2026-09-05T09:02:00Z",
                exported_at="2026-09-05T09:03:00Z")
    for row in rows:
        row.update(export_id=meta["export_id"], collected_at=meta["captured_to"], like_count=5)
        row["author"]["nickname"] = "更新昵称"
    statements = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(conn, "before_cursor_execute", observe)
    try:
        with freeze_batch(build_batch(rows, meta, tmp_path / "second"), tmp_path / "work") as batch:
            second = import_batch(conn, batch)
            assert import_batch(conn, batch)["state_id"] == second["state_id"]
    finally:
        event.remove(conn, "before_cursor_execute", observe)
    assert not any("INSERT INTO comment_payloads" in sql for sql in statements)
    assert conn.scalar(select(func.count()).select_from(comment_payloads)) == 3
    assert conn.scalar(select(func.count()).select_from(comments)) == 6
    conn.rollback()
    assert list_thread_comments(conn, first["state_id"], "100")["items"][0]["like_count"] == 0
    assert list_thread_comments(conn, second["state_id"], "100")["items"][0]["like_count"] == 5


def test_changed_payload_reclaimed_only_after_last_reference(conn, frozen_case, tmp_path):
    from uuid import uuid4

    from sqlalchemy import func, select

    from app.comment_export.export import build_batch
    from app.storage.frozen import freeze_batch
    from app.storage.importer import import_batch
    from app.storage.publication import publish_state
    from app.storage.schema import comment_payloads

    rows, meta = deepcopy(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "first"), tmp_path / "work") as batch:
        import_batch(conn, batch, publish=True)
    meta.update(export_id=str(uuid4()), captured_to="2026-09-05T09:02:00Z",
                exported_at="2026-09-05T09:03:00Z")
    for row in rows:
        row.update(export_id=meta["export_id"])
    rows[0]["content"]["text"] = "new body"
    with freeze_batch(build_batch(rows, meta, tmp_path / "second"), tmp_path / "work") as batch:
        result = import_batch(conn, batch)
    assert conn.scalar(select(func.count()).select_from(comment_payloads)) == 4
    conn.rollback()
    publish_state(conn, meta["video_id"], result["state_id"])
    assert conn.scalar(select(func.count()).select_from(comment_payloads)) == 3


def test_hash_collision_refuses_wrong_payload(conn, frozen_case, state_factory, monkeypatch):
    from app.storage import payloads
    from app.storage.errors import StorageError

    rows, meta = deepcopy(frozen_case)
    state_factory(conn, video_id=meta["video_id"])
    monkeypatch.setattr(payloads, "payload_digest", lambda _: "a" * 64)
    payloads.resolve_payloads(conn, meta["video_id"], rows[:1])
    rows[0]["content"]["text"] = "different body"
    with pytest.raises(StorageError, match="payload_hash_conflict"):
        payloads.resolve_payloads(conn, meta["video_id"], rows[:1])


def test_partial_replacement_reuses_old_working_payloads(conn, frozen_case, tmp_path):
    from uuid import uuid4

    from sqlalchemy import event

    from app.comment_export.export import build_batch
    from app.storage.frozen import freeze_batch
    from app.storage.importer import import_batch

    rows, meta = deepcopy(frozen_case)
    meta["coverage"].update(status="partial", replies_pagination="partial",
                            reasons=["replies_incomplete"])
    meta["_threads"]["100"]["pagination_status"] = "partial"
    with freeze_batch(build_batch(rows, meta, tmp_path / "first"), tmp_path / "work") as batch:
        assert import_batch(conn, batch)["receipt_status"] == "partial"
    meta.update(export_id=str(uuid4()), captured_to="2026-09-05T09:02:00Z",
                exported_at="2026-09-05T09:03:00Z")
    for row in rows:
        row["export_id"] = meta["export_id"]
    inserts = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO comment_payloads" in statement:
            inserts.append(statement)

    event.listen(conn, "before_cursor_execute", observe)
    try:
        with freeze_batch(build_batch(rows, meta, tmp_path / "second"), tmp_path / "work") as batch:
            assert import_batch(conn, batch)["receipt_status"] == "partial"
    finally:
        event.remove(conn, "before_cursor_execute", observe)
    assert inserts == []


def test_prune_preserves_other_videos_orphan_payloads(conn, frozen_case, state_factory):
    from sqlalchemy import select

    from app.storage.payloads import prune_payloads, resolve_payloads
    from app.storage.schema import comment_payloads

    rows, meta = deepcopy(frozen_case)
    state_factory(conn, video_id=meta["video_id"])
    own = resolve_payloads(conn, meta["video_id"], rows[:1])
    other = deepcopy(rows[0])
    other["video_id"] = "bilibili:video:2"
    state_factory(conn, video_id=other["video_id"])
    other_ids = resolve_payloads(conn, other["video_id"], [other])
    assert prune_payloads(conn, meta["video_id"]) == 1
    remaining = set(conn.execute(select(comment_payloads.c.payload_id)).scalars())
    assert remaining == set(other_ids.values())
    assert not remaining.intersection(own.values())


@pytest.mark.parametrize("number", [1e20, -0.0])
def test_jsonb_numeric_representation_does_not_break_import(conn, frozen_case, tmp_path, number):
    from uuid import uuid4

    from sqlalchemy import func, select

    from app.comment_export.export import build_batch
    from app.storage.frozen import freeze_batch
    from app.storage.importer import import_batch
    from app.storage.schema import comment_payloads

    rows, meta = frozen_case
    rows[0]["numeric_extension"] = number
    with freeze_batch(build_batch(rows, meta, tmp_path / "batch"), tmp_path / "work") as batch:
        assert import_batch(conn, batch, publish=True)["published"]
    meta.update(export_id=str(uuid4()), captured_to="2026-09-05T09:02:00Z",
                exported_at="2026-09-05T09:03:00Z")
    for row in rows:
        row["export_id"] = meta["export_id"]
    with freeze_batch(build_batch(rows, meta, tmp_path / "again"), tmp_path / "work") as batch:
        assert import_batch(conn, batch)["receipt_status"] == "ready"
    assert conn.scalar(select(func.count()).select_from(comment_payloads)) == 3


def test_failed_import_reclaims_only_orphans_and_reuses_written_bodies(
    conn, frozen_case, tmp_path, monkeypatch,
):
    from sqlalchemy import event, func, select

    from app.comment_export.export import build_batch
    from app.storage import importer
    from app.storage.errors import StorageError
    from app.storage.frozen import freeze_batch
    from app.storage.payloads import resolve_payloads
    from app.storage.schema import comment_payloads

    rows, meta = frozen_case
    original_load = importer._load

    def fail_after_committed_rows(connection, batch, state_id):
        original_load(connection, batch, state_id)
        orphan = deepcopy(rows[0])
        orphan["comment_id"] = "999"
        with connection.begin():
            resolve_payloads(connection, meta["video_id"], [orphan])
        raise StorageError("import_verification_failed")

    source = build_batch(rows, meta, tmp_path / "batch")
    with freeze_batch(source, tmp_path / "work") as batch:
        monkeypatch.setattr(importer, "_load", fail_after_committed_rows)
        with pytest.raises(StorageError, match="import_verification_failed"):
            importer.import_batch(conn, batch)
        assert conn.scalar(select(func.count()).select_from(comment_payloads)) == 3
        conn.rollback()
        monkeypatch.setattr(importer, "_load", original_load)
        inserts = []

        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            if "INSERT INTO comment_payloads" in statement:
                inserts.append(statement)

        event.listen(conn, "before_cursor_execute", observe)
        try:
            assert importer.import_batch(conn, batch)["receipt_status"] == "ready"
        finally:
            event.remove(conn, "before_cursor_execute", observe)
        assert inserts == []
