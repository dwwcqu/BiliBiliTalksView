from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.comment_export.export import build_batch
from app.comment_export.incremental import checked_thread, start_tracking, track_rows
from app.storage.frozen import freeze_batch
from app.storage.importer import import_batch
from app.storage.queries import read_state
from app.storage.schema import videos


def evidence_case(frozen_case):
    rows, meta = deepcopy(frozen_case)
    start_tracking(meta)
    track_rows(meta, rows, "replies")
    track_rows(meta, [r for r in rows if r["kind"] == "root"], "main")
    meta["_refresh"].update(
        last_full_scan_completed_at=meta["captured_to"], full_scan_incomplete=False
    )
    for row in rows:
        if row["kind"] == "root":
            checked_thread(meta["_threads"][row["root_id"]], row, meta["captured_to"])
    return rows, meta


def test_materialize_binds_context_and_publishes_atomically(conn, frozen_case, tmp_path):
    from app.storage.handoff import materialize, prepare_handoff

    rows, meta = evidence_case(frozen_case)
    path = build_batch(rows, meta, tmp_path / "batch")
    calls = []
    with freeze_batch(path, tmp_path / "freeze") as frozen:
        handoff = prepare_handoff(frozen, rows, meta, str(uuid4()), 0)
        result = materialize(conn, frozen, handoff, lambda connection, result: calls.append(result))
    assert result["published"]
    assert calls[0]["state_id"] == result["state_id"]
    assert read_state(conn, meta["video_id"])["state_id"] == result["state_id"]


def test_guard_failure_preserves_partial_cache(conn, frozen_case, tmp_path):
    from app.storage.errors import StorageError
    from app.storage.handoff import materialize, prepare_handoff

    rows, meta = evidence_case(frozen_case)
    meta["coverage"].update(
        status="partial", replies_pagination="partial", reasons=["replies_incomplete"]
    )
    meta["_threads"]["100"]["pagination_status"] = "partial"
    first = build_batch(rows, meta, tmp_path / "first")
    with freeze_batch(first, tmp_path / "freeze") as frozen:
        old = import_batch(conn, frozen)
    with conn.begin():
        version = conn.scalar(select(videos.c.cache_version))
    meta.update(
        export_id=str(uuid4()),
        captured_to="2026-09-05T09:02:00Z",
        exported_at="2026-09-05T09:03:00Z",
    )
    for row in rows:
        row["export_id"] = meta["export_id"]
    second = build_batch(rows, meta, tmp_path / "second")

    def reject(connection, result):
        assert connection.in_transaction()
        raise StorageError("lease_lost")

    with freeze_batch(second, tmp_path / "freeze") as frozen:
        handoff = prepare_handoff(frozen, rows, meta, str(uuid4()), version)
        with pytest.raises(StorageError, match="lease_lost"):
            materialize(conn, frozen, handoff, reject)
    assert read_state(conn, meta["video_id"])["state_id"] == old["state_id"]
    with conn.begin():
        assert conn.scalar(select(videos.c.cache_version)) == version


def test_atomic_load_keeps_old_partial_visible_and_rolls_back(
    conn, db_engine, frozen_case, tmp_path, monkeypatch
):
    from app.storage import importer
    from app.storage.errors import StorageError
    from app.storage.handoff import materialize, prepare_handoff

    rows, meta = evidence_case(frozen_case)
    meta["coverage"].update(
        status="partial", replies_pagination="partial", reasons=["replies_incomplete"]
    )
    meta["_threads"]["100"]["pagination_status"] = "partial"
    with freeze_batch(build_batch(rows, meta, tmp_path / "old"), tmp_path / "freeze") as frozen:
        old = import_batch(conn, frozen)
    with conn.begin():
        version = conn.scalar(select(videos.c.cache_version))
    meta.update(
        export_id=str(uuid4()),
        captured_to="2026-09-05T09:02:00Z",
        exported_at="2026-09-05T09:03:00Z",
    )
    for row in rows:
        row["export_id"] = meta["export_id"]
    load = importer._load
    observations = []

    def fail_after_load(connection, frozen, state_id, *, atomic=False):
        load(connection, frozen, state_id, atomic=atomic)
        with db_engine.connect() as reader:
            observations.append(read_state(reader, meta["video_id"])["state_id"])
        raise StorageError("import_verification_failed")

    monkeypatch.setattr(importer, "_load", fail_after_load)
    with freeze_batch(build_batch(rows, meta, tmp_path / "new"), tmp_path / "freeze") as frozen:
        handoff = prepare_handoff(frozen, rows, meta, str(uuid4()), version)
        with pytest.raises(StorageError, match="import_verification_failed"):
            materialize(conn, frozen, handoff, lambda *_: pytest.fail("guard before verification"))
    assert observations == [old["state_id"]]
    assert read_state(conn, meta["video_id"])["state_id"] == old["state_id"]


def test_handoff_retries_are_idempotent_but_other_job_is_rejected(conn, frozen_case, tmp_path):
    from app.storage.errors import StorageError
    from app.storage.handoff import materialize, prepare_handoff

    rows, meta = evidence_case(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "batch"), tmp_path / "freeze") as frozen:
        handoff = prepare_handoff(frozen, rows, meta, str(uuid4()), 0)
        first = materialize(conn, frozen, handoff, lambda *_: None)
        with conn.begin():
            version = conn.scalar(select(videos.c.cache_version))
        second = materialize(conn, frozen, handoff, lambda *_: None)
        assert first == second
        with conn.begin():
            assert conn.scalar(select(videos.c.cache_version)) == version
        other = prepare_handoff(frozen, rows, meta, str(uuid4()), version)
        with pytest.raises(StorageError, match="handoff_context_conflict"):
            materialize(conn, frozen, other, lambda *_: None)


def test_changed_baseline_rejects_stale_result(conn, frozen_case, tmp_path):
    from app.storage.errors import StorageError
    from app.storage.handoff import materialize, prepare_handoff

    rows, meta = evidence_case(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "first"), tmp_path / "freeze") as frozen:
        old = import_batch(conn, frozen, publish=True)
    meta.update(
        export_id=str(uuid4()),
        captured_to="2026-09-05T09:02:00Z",
        exported_at="2026-09-05T09:03:00Z",
    )
    for row in rows:
        row["export_id"] = meta["export_id"]
    with freeze_batch(build_batch(rows, meta, tmp_path / "second"), tmp_path / "freeze") as frozen:
        handoff = prepare_handoff(frozen, rows, meta, str(uuid4()), 0)
        with pytest.raises(StorageError, match="baseline_changed"):
            materialize(conn, frozen, handoff, lambda *_: pytest.fail("stale result reached guard"))
    assert read_state(conn, meta["video_id"])["state_id"] == old["state_id"]


def test_handoff_rejects_mismatched_records_before_writes(frozen_case, tmp_path):
    from app.storage.errors import StorageError
    from app.storage.handoff import prepare_handoff

    rows, meta = evidence_case(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "batch"), tmp_path / "freeze") as frozen:
        rows[0]["content"]["text"] = "different"
        with pytest.raises(StorageError, match="invalid_collection_handoff"):
            prepare_handoff(frozen, rows, meta, str(uuid4()), 0)


def test_frozen_baseline_keeps_evidence_after_original_state_reclaimed(conn, frozen_case, tmp_path):
    from app.comment_export.checkpoint import read_checkpoint
    from app.comment_export.incremental import prepare_refresh
    from app.storage.baseline import freeze_baseline
    from app.storage.handoff import materialize, prepare_handoff
    from app.storage.schema import discussion_states

    rows, meta = evidence_case(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "first"), tmp_path / "freeze") as frozen:
        handoff = prepare_handoff(frozen, rows, meta, str(uuid4()), 0)
        old = materialize(conn, frozen, handoff, lambda *_: None)
    baseline = freeze_baseline(conn, meta["video_id"], tmp_path / "baseline")
    assert baseline["state_id"] == old["state_id"]
    saved, metadata, progress = read_checkpoint(tmp_path / "baseline")
    _, fresh, _ = prepare_refresh(saved, metadata, progress, "auto", "2026-09-05T10:00:00Z", 24)
    assert fresh["_refresh"]["mode"] == "incremental"
    assert fresh["_threads"]["100"]["reply_check_state"] == "complete"
    assert "main_core" not in handoff.context_json and "observed_core" not in handoff.context_json
    meta.update(
        export_id=str(uuid4()),
        captured_to="2026-09-05T09:02:00Z",
        exported_at="2026-09-05T09:03:00Z",
    )
    for row in rows:
        row["export_id"] = meta["export_id"]
    with freeze_batch(build_batch(rows, meta, tmp_path / "second"), tmp_path / "freeze") as frozen:
        import_batch(conn, frozen, publish=True)
    with conn.begin():
        assert (
            conn.scalar(
                select(discussion_states.c.state_id).where(
                    discussion_states.c.state_id == old["state_id"]
                )
            )
            is None
        )
    assert read_checkpoint(tmp_path / "baseline")[0] == saved


def test_full_completion_evidence_cannot_include_unchecked_floor(frozen_case, tmp_path):
    from app.storage.errors import StorageError
    from app.storage.handoff import prepare_handoff

    rows, meta = evidence_case(frozen_case)
    meta["_threads"]["100"].update(
        reply_check_state="needs_check", reply_verification="not_checked"
    )
    with (
        freeze_batch(build_batch(rows, meta, tmp_path / "batch"), tmp_path / "freeze") as frozen,
        pytest.raises(StorageError, match="invalid_collection_handoff"),
    ):
        prepare_handoff(frozen, rows, meta, str(uuid4()), 0)


def test_false_guard_result_cannot_commit(conn, frozen_case, tmp_path):
    from app.storage.errors import StorageError
    from app.storage.handoff import materialize, prepare_handoff

    rows, meta = evidence_case(frozen_case)
    with freeze_batch(build_batch(rows, meta, tmp_path / "batch"), tmp_path / "freeze") as frozen:
        handoff = prepare_handoff(frozen, rows, meta, str(uuid4()), 0)
        with pytest.raises(StorageError, match="commit_guard_rejected"):
            materialize(conn, frozen, handoff, lambda *_: False)
    with conn.begin():
        assert conn.scalar(select(videos.c.video_id)) is None
