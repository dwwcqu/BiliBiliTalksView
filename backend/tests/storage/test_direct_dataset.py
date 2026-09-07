from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_handoff import evidence_case

from app.comment_export.export import build_batch, build_dataset
from app.storage.errors import StorageError
from app.storage.frozen import freeze_batch
from app.storage.handoff import materialize, prepare_dataset_handoff, prepare_handoff
from app.storage.schema import import_receipts, videos
from app.storage.semantic import from_frozen


def test_direct_handoff_snapshot_matches_file(frozen_case, tmp_path):
    rows, meta = evidence_case(frozen_case)
    dataset = build_dataset(rows, meta)
    job_id = str(uuid4())
    expected = prepare_dataset_handoff(dataset, job_id, 0)
    with freeze_batch(build_batch(rows, meta, tmp_path / 'batch'), tmp_path / 'freeze') as frozen:
        assert dataset.digest == frozen.digest
        assert from_frozen(dataset) == from_frozen(frozen)
        assert expected == prepare_handoff(frozen, rows, meta, job_id, 0)
    rows[0]['content'] = 'changed'
    meta['_refresh']['observed_ids'].clear()
    dataset.evidence['_threads'].clear()
    assert prepare_dataset_handoff(dataset, job_id, 0) == expected


@pytest.mark.parametrize('reject', [False, 'lease_lost'])
def test_direct_guard_rollback(conn, frozen_case, reject):
    rows, meta = evidence_case(frozen_case)
    dataset = build_dataset(rows, meta)
    handoff = prepare_dataset_handoff(dataset, str(uuid4()), 0)

    def guard(connection, result):
        assert connection.in_transaction()
        if reject:
            raise StorageError(reject)
        return False

    with pytest.raises(StorageError, match=reject or 'commit_guard_rejected'):
        materialize(conn, dataset, handoff, guard)
    with conn.begin():
        assert conn.scalar(select(func.count()).select_from(videos)) == 0
        assert conn.scalar(select(func.count()).select_from(import_receipts)) == 0


def test_direct_receipt_replay_and_context_conflict(conn, frozen_case):
    rows, meta = evidence_case(frozen_case)
    dataset = build_dataset(rows, meta)
    handoff = prepare_dataset_handoff(dataset, str(uuid4()), 0)
    result = materialize(conn, dataset, handoff, lambda *_: None)
    assert result['published']
    assert materialize(conn, dataset, handoff, lambda *_: None) == result
    other = prepare_dataset_handoff(dataset, str(uuid4()), 0)
    with pytest.raises(StorageError, match='handoff_context_conflict'):
        materialize(conn, dataset, other, lambda *_: None)
    later_id = str(uuid4())
    later = build_dataset([dict(row, export_id=later_id) for row in rows],
                          dict(meta, export_id=later_id))
    stale = prepare_dataset_handoff(later, str(uuid4()), 0)
    with pytest.raises(StorageError, match='baseline_changed'):
        materialize(conn, later, stale, lambda *_: None)


def test_worker_save_result_has_no_export_files(frozen_case, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from app.comment_export import export
    from app.jobs import work
    from app.storage import frozen

    rows, meta = evidence_case(frozen_case)
    job_id = str(uuid4())
    export_id = uuid4()
    monkeypatch.setattr(work, 'read_checkpoint', lambda _: (rows, meta, {}))
    monkeypatch.setattr(work, 'uuid4', lambda: export_id)
    monkeypatch.setattr(work, 'now', lambda: meta['exported_at'])

    def forbidden(*args, **kwargs):
        pytest.fail('filesystem export on direct worker path')

    monkeypatch.setattr(work, 'TemporaryDirectory', forbidden)
    monkeypatch.setattr(export, 'build_batch', forbidden)
    monkeypatch.setattr(frozen, 'freeze_batch', forbidden)
    expected_rows = [dict(r, schema_version='2.0.0', export_id=str(export_id)) for r in rows]
    expected = build_dataset(expected_rows, dict(meta, schema_version='2.0.0',
                                               export_id=str(export_id)))
    owner = SimpleNamespace(engine=SimpleNamespace(connect=lambda: nullcontext('connection')))
    claim = {'id': job_id, 'video_id': meta['video_id']}
    calls = []
    monkeypatch.setattr(work, 'complete_job', lambda *args: calls.append(args))

    def capture(connection, dataset, handoff, guard):
        assert not hasattr(dataset, 'directory')
        assert connection == 'connection'
        assert dataset.digest == expected.digest
        assert handoff == prepare_dataset_handoff(expected, job_id, 0)
        guard(connection, {'published': True})
        return {'published': True}

    monkeypatch.setattr(work, 'materialize', capture)
    assert work.save_result(owner, claim, None, None, 0) == {'published': True}
    assert calls == [('connection', owner, claim, {'published': True})]


def test_direct_tail_context_matches_file(frozen_case, tmp_path):
    from test_tail_handoff import tail_case

    rows, meta = tail_case(frozen_case)
    dataset = build_dataset(rows, meta)
    job_id = str(uuid4())
    with freeze_batch(build_batch(rows, meta, tmp_path / 'batch'), tmp_path / 'freeze') as frozen:
        assert prepare_dataset_handoff(dataset, job_id, 0) == prepare_handoff(
            frozen, rows, meta, job_id, 0)


def test_direct_zero_context_matches_file(monkeypatch, tmp_path):
    from test_zero_handoff import observed_case

    rows, meta = observed_case(monkeypatch, tmp_path)
    dataset = build_dataset(rows, meta)
    job_id = meta['_refresh']['zero_policy_snapshot']['work_id']
    with freeze_batch(build_batch(rows, meta, tmp_path / 'batch'), tmp_path / 'freeze') as frozen:
        assert prepare_dataset_handoff(dataset, job_id, 0) == prepare_handoff(
            frozen, rows, meta, job_id, 0)
