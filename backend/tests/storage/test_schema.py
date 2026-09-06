from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.storage.schema import comment_payloads, comments, discussion_states, threads, videos


def video(db, aid="1"):
    value = f"bilibili:video:{aid}"
    db.execute(
        insert(videos).values(video_id=value, platform="bilibili", aid=aid, oid=aid, comment_type=1)
    )
    return value


def state(db, video_id, lifecycle="loading"):
    sid = str(uuid4())
    stamp = datetime(2026, 9, 6, tzinfo=UTC)
    db.execute(
        insert(discussion_states).values(
            state_id=sid,
            video_id=video_id,
            source_export_id=str(uuid4()),
            schema_version="1.0.0",
            hour_bucket=stamp,
            captured_from=stamp,
            captured_to=stamp,
            exported_at=stamp,
            coverage={"status": "partial"},
            source_metadata={},
            lifecycle=lifecycle,
        )
    )
    return sid


def thread(db, sid):
    db.execute(
        insert(threads).values(
            state_id=sid,
            root_id="1",
            comment_count=1,
            reply_count=1,
            participant_count=0,
            unknown_author_comment_count=1,
            coverage={},
            source_metadata={},
        )
    )


def comment(db, sid, cid="999999999999999999999999999"):
    vid = db.scalar(select(discussion_states.c.video_id).where(discussion_states.c.state_id == sid))
    pid = str(uuid4())
    db.execute(insert(comment_payloads).values(
        payload_id=pid, video_id=vid, comment_id=cid,
        content_hash="a" * 64, content={}, extra_fields={},
    ))
    return {
        "video_id": vid, "payload_id": pid,
        "state_id": sid,
        "comment_id": cid,
        "root_id": "1",
        "parent_id": "888",
        "kind": "reply",
        "author_uid": None,
        "collected_at": datetime(2026, 9, 6, tzinfo=UTC),
        "reply_relation": {"status": "source", "target_uid": None},
    }


def test_missing_parent_allowed_and_generated_order_columns(db):
    sid = state(db, video(db))
    thread(db, sid)
    value = comment(db, sid)
    db.execute(insert(comments).values(**value))
    row = db.execute(select(comments)).mappings().one()
    assert row["parent_id"] == "888"
    assert row["id_length"] == len(value["comment_id"])
    assert row["root_rank"] == 1
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(insert(comments).values(**value))


def test_second_working_state_rejected(db):
    vid = video(db)
    state(db, vid, "partial")
    with pytest.raises(IntegrityError), db.begin_nested():
        state(db, vid, "failed")


def test_cross_video_pointer_and_equal_pointers_rejected(db):
    first, second = video(db), video(db, "2")
    sid = state(db, first)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(update(videos).where(videos.c.video_id == second).values(current_state_id=sid))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            update(videos)
            .where(videos.c.video_id == first)
            .values(current_state_id=sid, working_state_id=sid)
        )


@pytest.mark.parametrize(
    "column,value",
    [("comment_id", "01"), ("root_id", "0"), ("author_uid", "-1"), ("parent_id", "1.5")],
)
def test_invalid_external_ids_rejected(db, column, value):
    sid = state(db, video(db))
    thread(db, sid)
    row = comment(db, sid)
    row[column] = value
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(insert(comments).values(**row))


def test_upgrade_is_idempotent_and_test_schema_downgrade_rebuilds(db, migration_config):
    from alembic import command
    from sqlalchemy import inspect

    migration_config.attributes["connection"] = db
    command.upgrade(migration_config, "head")
    assert len(inspect(db).get_table_names()) == 13  # Twelve domain tables and Alembic version.
    command.downgrade(migration_config, "base")
    assert inspect(db).get_table_names() == ["alembic_version"]
    command.upgrade(migration_config, "head")
    assert len(inspect(db).get_table_names()) == 13


def test_state_delete_cascades_body_but_preserves_video(db):
    from sqlalchemy import delete, func

    vid = video(db)
    sid = state(db, vid)
    thread(db, sid)
    db.execute(insert(comments).values(**comment(db, sid)))
    db.execute(delete(discussion_states).where(discussion_states.c.state_id == sid))
    assert db.scalar(select(func.count()).select_from(comments)) == 0
    assert db.scalar(select(func.count()).select_from(threads)) == 0
    assert db.scalar(select(func.count()).select_from(videos)) == 1


def test_receipt_reference_must_be_cleared_before_state_delete(db):
    from sqlalchemy import delete

    from app.storage.schema import import_receipts

    vid = video(db)
    sid = state(db, vid)
    db.execute(
        insert(import_receipts).values(
            video_id=vid,
            source_export_id=str(uuid4()),
            canonical_digest="a" * 64,
            state_id=sid,
            status="ready",
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(delete(discussion_states).where(discussion_states.c.state_id == sid))


def test_invalid_lifecycle_rejected(db):
    vid = video(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        state(db, vid, "unexpected")


def test_thread_index_has_nulls_last_and_comment_id_c_collation(db):
    from sqlalchemy import text

    indexes = (
        db.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                "AND indexname = 'ix_comments_thread_order'"
            )
        )
        .scalars()
        .all()
    )
    assert len(indexes) == 1
    # ASC NULLS LAST is PostgreSQL's default and may be omitted in pg_get_indexdef output.
    assert "root_rank, created_at, id_length, comment_id" in indexes[0]
    collation = db.scalar(
        text(
            "SELECT collation_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='comments' "
            "AND column_name='comment_id'"
        )
    )
    assert collation == "C"


def test_payload_reference_cannot_cross_comment_or_video(db):
    from uuid import uuid4

    vid = video(db)
    sid = state(db, vid)
    thread(db, sid)
    row = comment(db, sid)
    for other_video, other_comment in [(vid, "22"), (video(db, "2"), row["comment_id"])]:
        pid = str(uuid4())
        db.execute(insert(comment_payloads).values(
            payload_id=pid, video_id=other_video, comment_id=other_comment,
            content_hash="b" * 64, content={}, extra_fields={},
        ))
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(insert(comments).values(**{**row, "payload_id": pid}))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(insert(comments).values(**{**row, "video_id": "bilibili:video:2"}))
