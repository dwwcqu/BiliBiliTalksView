"""Data-bearing upgrade and downgrade coverage for reusable comment payloads."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.storage.codec import decode_json, encode_json
from app.storage.payloads import payload_digest

REVISION_0001 = "0001_discussion_storage"
REVISION_0002 = "0002_comment_payloads"


def _insert_legacy_fixture(connection):
    video_id = "bilibili:video:42"
    current_state = uuid4()
    working_state = uuid4()
    now = datetime(2026, 9, 6, tzinfo=UTC)
    connection.execute(
        text(
            "INSERT INTO videos "
            "(video_id, platform, aid, oid, comment_type, current_state_id, working_state_id) "
            "VALUES (:video_id, 'bilibili', '42', '42', 1, NULL, NULL)"
        ),
        {"video_id": video_id},
    )
    state_sql = text(
        "INSERT INTO discussion_states "
        "(state_id, video_id, source_export_id, schema_version, hour_bucket, captured_from, "
        "captured_to, exported_at, coverage, source_metadata, lifecycle) VALUES "
        "(:state_id, :video_id, :source_export_id, '2.0.0', :stamp, :stamp, :stamp, :stamp, "
        "CAST(:coverage AS jsonb), CAST(:source_metadata AS jsonb), :lifecycle)"
    )
    for state_id, lifecycle in ((current_state, "current"), (working_state, "loading")):
        connection.execute(
            state_sql,
            {
                "state_id": state_id,
                "video_id": video_id,
                "source_export_id": uuid4(),
                "stamp": now,
                "coverage": '{"status":"verified"}',
                "source_metadata": "{}",
                "lifecycle": lifecycle,
            },
        )
        connection.execute(
            text(
                "INSERT INTO threads "
                "(state_id, root_id, comment_count, reply_count, participant_count, "
                "unknown_author_comment_count, coverage, source_metadata) VALUES "
                "(:state_id, '100', 2, 1, 1, 0, '{}'::jsonb, '{}'::jsonb)"
            ),
            {"state_id": state_id},
        )
    connection.execute(
        text(
            "UPDATE videos SET current_state_id=:current_state, working_state_id=:working_state "
            "WHERE video_id=:video_id"
        ),
        {
            "current_state": current_state,
            "working_state": working_state,
            "video_id": video_id,
        },
    )

    # These are frozen pg-text-v1 documents, including NUL, backslash, a surrogate, and
    # unknown extension keys. Comment 100 is identical across states; 101 changes body.
    special_content = json.dumps(
        encode_json({"message": "same\\path\x00\ud800"}), ensure_ascii=True
    )
    special_extras = json.dumps(
        encode_json({"unknown\x00": "value\\", "large": 1e20, "negative_zero": -0.0}),
        ensure_ascii=True,
    )
    rows = [
        (current_state, "100", special_content, special_extras),
        (working_state, "100", special_content, special_extras),
        (current_state, "101", '{"message":"before"}', '{"nested":{"flag":true}}'),
        (working_state, "101", '{"message":"after"}', '{"nested":{"flag":true}}'),
    ]
    comment_sql = text(
        "INSERT INTO comments "
        "(state_id, comment_id, root_id, parent_id, kind, author_uid, nickname, created_at, "
        "collected_at, like_count, reply_relation, content, extra_fields) VALUES "
        "(:state_id, :comment_id, '100', :parent_id, :kind, '7', 'tester', :stamp, :stamp, 1, "
        "'{}'::jsonb, CAST(:content AS jsonb), CAST(:extra_fields AS jsonb))"
    )
    for state_id, comment_id, content, extra_fields in rows:
        connection.execute(
            comment_sql,
            {
                "state_id": state_id,
                "comment_id": comment_id,
                "parent_id": None if comment_id == "100" else "100",
                "kind": "root" if comment_id == "100" else "reply",
                "stamp": now,
                "content": content,
                "extra_fields": extra_fields,
            },
        )
    return video_id, current_state, working_state


def _decoded_rows(connection):
    rows = connection.execute(
        text(
            "SELECT c.state_id, c.comment_id, p.payload_id, p.content, p.extra_fields "
            "FROM comments c JOIN comment_payloads p "
            "ON p.video_id=c.video_id AND p.comment_id=c.comment_id "
            "AND p.payload_id=c.payload_id ORDER BY c.state_id, c.comment_id"
        )
    ).mappings()
    return [
        (
            row["state_id"],
            row["comment_id"],
            row["payload_id"],
            decode_json(row["content"]),
            decode_json(row["extra_fields"]),
        )
        for row in rows
    ]


def test_data_upgrade_downgrade_and_reupgrade_are_lossless(db, migration_config):
    migration_config.attributes["connection"] = db
    command.downgrade(migration_config, REVISION_0001)
    video_id, current_state, working_state = _insert_legacy_fixture(db)

    command.upgrade(migration_config, REVISION_0002)

    columns = {column["name"] for column in inspect(db).get_columns("comments")}
    assert "content" not in columns
    assert "extra_fields" not in columns
    assert {"video_id", "payload_id"} <= columns
    upgraded = _decoded_rows(db)
    assert len(upgraded) == 4
    same = [row for row in upgraded if row[1] == "100"]
    changed = [row for row in upgraded if row[1] == "101"]
    assert same[0][2] == same[1][2]
    assert changed[0][2] != changed[1][2]
    assert same[0][3] == {"message": "same\\path\0\ud800"}
    assert same[0][4] == {
        "unknown\0": "value\\", "large": 100000000000000000000, "negative_zero": 0,
    }
    assert {row[0] for row in upgraded} == {current_state, working_state}
    assert db.scalar(text("SELECT count(*) FROM comment_payloads")) == 3
    payloads = db.execute(
        text("SELECT content_hash, content, extra_fields FROM comment_payloads")
    ).mappings()
    for payload in payloads:
        document = {
            "content": decode_json(payload["content"]),
            "extra_fields": decode_json(payload["extra_fields"]),
        }
        assert payload["content_hash"] == payload_digest(document)

    foreign_keys = {fk["name"] for fk in inspect(db).get_foreign_keys("comments")}
    assert {"fk_comment_state", "fk_comment_payload"} <= foreign_keys
    indexes = {index["name"] for index in inspect(db).get_indexes("comments")}
    assert "ix_comments_payload" in indexes

    wrong_payload = db.execute(
        text("SELECT payload_id FROM comment_payloads WHERE comment_id='100'")
    ).scalar_one()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "UPDATE comments SET payload_id=:payload_id WHERE state_id=:state_id "
                "AND comment_id='101'"
            ),
            {"payload_id": wrong_payload, "state_id": current_state},
        )

    command.downgrade(migration_config, REVISION_0001)
    legacy_columns = {column["name"] for column in inspect(db).get_columns("comments")}
    assert {"content", "extra_fields"} <= legacy_columns
    assert "payload_id" not in legacy_columns
    legacy = db.execute(
        text(
            "SELECT state_id, comment_id, content, extra_fields FROM comments "
            "ORDER BY state_id, comment_id"
        )
    ).mappings()
    decoded_legacy = [
        (
            row["state_id"],
            row["comment_id"],
            decode_json(row["content"]),
            decode_json(row["extra_fields"]),
        )
        for row in legacy
    ]
    assert [(row[0], row[1], row[3], row[4]) for row in upgraded] == decoded_legacy

    command.upgrade(migration_config, REVISION_0002)
    reupgraded = _decoded_rows(db)
    assert [(row[0], row[1], row[3], row[4]) for row in reupgraded] == [
        (row[0], row[1], row[3], row[4]) for row in upgraded
    ]
    assert (
        db.scalar(
            text("SELECT count(*) FROM comments WHERE video_id=:video_id"), {"video_id": video_id}
        )
        == 4
    )
    assert all(isinstance(row[2], UUID) for row in reupgraded)


def test_failed_upgrade_rolls_back_all_schema_and_data_changes(db, migration_config):
    migration_config.attributes["connection"] = db
    command.downgrade(migration_config, REVISION_0001)
    _, current_state, _ = _insert_legacy_fixture(db)
    invalid_stored_json = json.dumps({"message": r"\q"})
    db.execute(
        text(
            "UPDATE comments SET content=CAST(:content AS jsonb) "
            "WHERE state_id=:state_id AND comment_id='100'"
        ),
        {"content": invalid_stored_json, "state_id": current_state},
    )

    db.commit()
    with pytest.raises(RuntimeError, match="payload_migration_decode_error"), db.begin():
        command.upgrade(migration_config, REVISION_0002)

    assert db.scalar(text("SELECT version_num FROM alembic_version")) == REVISION_0001
    assert "comment_payloads" not in inspect(db).get_table_names()
    columns = {column["name"] for column in inspect(db).get_columns("comments")}
    assert {"content", "extra_fields"} <= columns
    assert {"video_id", "payload_id"}.isdisjoint(columns)
    assert (
        db.scalar(
            text(
                "SELECT content->>'message' FROM comments "
                "WHERE state_id=:state_id AND comment_id='100'"
            ),
            {"state_id": current_state},
        )
        == r"\q"
    )
