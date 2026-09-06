"""State-scoped queries against real PostgreSQL."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, update

from app.storage.errors import StorageError
from app.storage.mapping import member_values
from app.storage.payloads import prune_payloads, resolve_payloads
from app.storage.queries import list_thread_comments, list_user_comments, read_snapshot, read_state
from app.storage.schema import comments, discussion_states, threads, videos


@pytest.fixture
def query_state(conn, state_factory, frozen_case):
    rows, _ = frozen_case
    root = deepcopy(rows[-1])
    root["created_at"] = None
    root["content"]["text"] = "真实\0以及字面\\0"
    replies = []
    for cid, stamp in [
        ("9", "2026-09-05T08:00:00Z"),
        ("100000000000000000000000", "2026-09-05T08:00:00Z"),
        ("300", None),
    ]:
        row = deepcopy(root)
        row.update(comment_id=cid, kind="reply", parent_id="100", created_at=stamp)
        row["reply_relation"]["status"] = "source"
        replies.append(row)
    state = state_factory(conn, video_id=root["video_id"], lifecycle="partial")
    conn.execute(
        insert(threads).values(
            state_id=state["state_id"],
            root_id="100",
            comment_count=4,
            reply_count=3,
            participant_count=1,
            unknown_author_comment_count=0,
            coverage={},
            source_metadata={},
        )
    )
    payload_ids = resolve_payloads(conn, state["video_id"], [root, *replies])
    conn.execute(insert(comments), [
        member_values(row, state["state_id"], payload_ids[row["comment_id"]])
        for row in [root, *replies]
    ])
    conn.execute(
        update(videos)
        .where(videos.c.video_id == state["video_id"])
        .values(working_state_id=state["state_id"])
    )
    conn.commit()
    return state


def test_keyset_root_first_numeric_ids_and_null_times(conn, query_state):
    ids, cursor = [], None
    while True:
        page = list_thread_comments(conn, query_state["state_id"], "100", limit=1, cursor=cursor)
        ids.extend(row["comment_id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert ids == ["100", "9", "100000000000000000000000", "300"]
    assert read_state(conn, query_state["video_id"])["lifecycle"] == "partial"


def test_user_order_and_cursor_scope(conn, query_state):
    state_id = query_state["state_id"]
    page = list_user_comments(conn, state_id, "1", limit=1)
    assert page["items"][0]["comment_id"] == "9"
    with pytest.raises(StorageError, match="invalid_cursor"):
        list_user_comments(conn, state_id, "2", cursor=page["next_cursor"])
    with pytest.raises(StorageError, match="invalid_cursor"):
        list_thread_comments(conn, state_id, "100", cursor=page["next_cursor"])
    assert list_user_comments(conn, state_id, None)["items"] == []
    result = list_thread_comments(conn, state_id, "100")
    assert result["items"][0]["content"]["text"] == "真实\0以及字面\\0"


def test_expired_and_invalid_limit(conn, query_state):
    with pytest.raises(StorageError, match="state_expired"):
        list_user_comments(conn, str(uuid4()), "1")
    with pytest.raises(StorageError, match="invalid_limit"):
        list_user_comments(conn, query_state["state_id"], "1", limit=0)


def test_repeatable_reader_survives_reclamation(conn, db_engine, query_state):
    sid = query_state["state_id"]
    with read_snapshot(conn):
        assert read_state(conn, query_state["video_id"])["state_id"] == sid
        with db_engine.begin() as writer:
            writer.execute(
                update(videos)
                .where(videos.c.video_id == query_state["video_id"])
                .values(working_state_id=None)
            )
            writer.execute(delete(discussion_states).where(discussion_states.c.state_id == sid))
            assert prune_payloads(writer, query_state["video_id"]) == 4
        assert len(list_thread_comments(conn, sid, "100")["items"]) == 4
    with pytest.raises(StorageError, match="state_expired"):
        list_thread_comments(conn, sid, "100")


def test_existing_read_committed_transaction_is_not_silently_committed(conn, query_state):
    from sqlalchemy import text

    with conn.begin():
        conn.execute(text("SELECT 1"))
        with pytest.raises(StorageError, match="snapshot_required"):
            read_state(conn, query_state["video_id"])
        assert conn.in_transaction()
