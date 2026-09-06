"""Migration and trigger coverage for cache handoff versioning."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

REVISION_0002 = "0002_comment_payloads"
REVISION_0003 = "0003_cache_handoff"


def _insert_video_and_state(connection):
    video_id = "bilibili:video:42"
    state_id = uuid4()
    stamp = datetime(2026, 9, 6, tzinfo=UTC)
    connection.execute(
        text(
            "INSERT INTO videos "
            "(video_id, platform, aid, oid, comment_type) "
            "VALUES (:video_id, 'bilibili', '42', '42', 1)"
        ),
        {"video_id": video_id},
    )
    connection.execute(
        text(
            "INSERT INTO discussion_states "
            "(state_id, video_id, source_export_id, schema_version, hour_bucket, captured_from, "
            "captured_to, exported_at, coverage, source_metadata, lifecycle) VALUES "
            "(:state_id, :video_id, :source_export_id, '2.0.0', :stamp, :stamp, :stamp, :stamp, "
            "'{\"status\":\"verified\"}'::jsonb, '{}'::jsonb, 'partial')"
        ),
        {
            "state_id": state_id,
            "video_id": video_id,
            "source_export_id": uuid4(),
            "stamp": stamp,
        },
    )
    connection.execute(
        text(
            "INSERT INTO threads "
            "(state_id, root_id, comment_count, reply_count, participant_count, "
            "unknown_author_comment_count, coverage, source_metadata) "
            "VALUES (:state_id, '100', 1, 0, 1, 0, '{}'::jsonb, '{}'::jsonb)"
        ),
        {"state_id": state_id},
    )
    payload_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO comment_payloads "
            "(payload_id, video_id, comment_id, content_hash, content, extra_fields) "
            "VALUES (:payload_id, :video_id, '100', :content_hash, "
            "CAST(:content AS jsonb), CAST(:extra_fields AS jsonb))"
        ),
        {
            "payload_id": payload_id,
            "video_id": video_id,
            "content_hash": "a" * 64,
            "content": '{"message":"kept"}',
            "extra_fields": '{"extension":true}',
        },
    )
    connection.execute(
        text(
            "INSERT INTO comments "
            "(state_id, video_id, comment_id, payload_id, root_id, kind, collected_at, "
            "reply_relation) VALUES "
            "(:state_id, :video_id, '100', :payload_id, '100', 'root', :stamp, '{}'::jsonb)"
        ),
        {
            "state_id": state_id,
            "video_id": video_id,
            "payload_id": payload_id,
            "stamp": stamp,
        },
    )
    return video_id, state_id, payload_id


def test_upgrade_and_downgrade_preserve_existing_membership_and_payload(
    db, migration_config
):
    migration_config.attributes["connection"] = db
    command.downgrade(migration_config, REVISION_0002)
    video_id, state_id, payload_id = _insert_video_and_state(db)

    command.upgrade(migration_config, REVISION_0003)

    assert db.scalar(
        text("SELECT cache_version FROM videos WHERE video_id=:video_id"),
        {"video_id": video_id},
    ) == 0
    assert db.scalar(
        text("SELECT refresh_context FROM discussion_states WHERE state_id=:state_id"),
        {"state_id": state_id},
    ) is None
    db.execute(
        text(
            "UPDATE discussion_states SET refresh_context=:context "
            "WHERE state_id=:state_id"
        ),
        {"context": '{"job_id":"job-1"}', "state_id": state_id},
    )

    command.downgrade(migration_config, REVISION_0002)

    assert "cache_version" not in {
        column["name"] for column in inspect(db).get_columns("videos")
    }
    assert "refresh_context" not in {
        column["name"] for column in inspect(db).get_columns("discussion_states")
    }
    row = db.execute(
        text(
            "SELECT c.state_id, c.payload_id, p.content, p.extra_fields "
            "FROM comments c JOIN comment_payloads p ON p.payload_id=c.payload_id "
            "WHERE c.state_id=:state_id"
        ),
        {"state_id": state_id},
    ).mappings().one()
    assert row == {
        "state_id": state_id,
        "payload_id": payload_id,
        "content": {"message": "kept"},
        "extra_fields": {"extension": True},
    }


def test_cache_version_tracks_visible_changes_and_rejects_regression(
    db, migration_config
):
    migration_config.attributes["connection"] = db
    command.downgrade(migration_config, REVISION_0002)
    video_id, state_id, _ = _insert_video_and_state(db)
    command.upgrade(migration_config, REVISION_0003)

    db.execute(
        text("UPDATE videos SET working_state_id=:state_id WHERE video_id=:video_id"),
        {"state_id": state_id, "video_id": video_id},
    )
    assert db.scalar(text("SELECT cache_version FROM videos")) == 1

    db.execute(text("UPDATE videos SET working_state_id=working_state_id"))
    db.execute(text("UPDATE discussion_states SET lifecycle=lifecycle, refresh_context=NULL"))
    assert db.scalar(text("SELECT cache_version FROM videos")) == 1

    db.execute(
        text(
            "UPDATE discussion_states SET refresh_context='{\"job_id\":\"job-1\"}'::jsonb "
            "WHERE state_id=:state_id"
        ),
        {"state_id": state_id},
    )
    assert db.scalar(text("SELECT cache_version FROM videos")) == 2
    db.execute(
        text("UPDATE discussion_states SET lifecycle='ready' WHERE state_id=:state_id"),
        {"state_id": state_id},
    )
    assert db.scalar(text("SELECT cache_version FROM videos")) == 3

    with db.begin_nested() as nested:
        db.execute(text("UPDATE videos SET current_state_id=working_state_id, working_state_id=NULL"))
        assert db.scalar(text("SELECT cache_version FROM videos")) == 4
        nested.rollback()
    assert db.scalar(text("SELECT cache_version FROM videos")) == 3

    db.execute(text("UPDATE videos SET current_state_id=working_state_id, working_state_id=NULL"))
    assert db.scalar(text("SELECT cache_version FROM videos")) == 4
    db.execute(
        text("UPDATE discussion_states SET lifecycle='current' WHERE state_id=:state_id"),
        {"state_id": state_id},
    )
    assert db.scalar(text("SELECT cache_version FROM videos")) == 5

    with (
        pytest.raises((DBAPIError, IntegrityError), match="cache_version_regression"),
        db.begin_nested(),
    ):
        db.execute(text("UPDATE videos SET cache_version=2"))
