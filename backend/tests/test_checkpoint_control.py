import pytest

from app.comment_export.checkpoint import Checkpoint
from app.comment_export.contract import ContractError


def test_request_reservation_survives_reopen_and_uses_actual_revision(tmp_path):
    path = tmp_path / 'work.sqlite3'
    cp = Checkpoint(path)
    metadata = {'video_id': '1'}
    progress = {'metadata': metadata, 'requests': 1, 'max_requests': 10}
    cp.commit_page('one', [], progress)
    result = cp.reserve_request({'checkpoint_revision': 999}, 8)
    assert result['attempt']['checkpoint_revision'] == 1
    assert progress['metadata'] is metadata
    cp.close()
    cp = Checkpoint(path)
    assert cp.read_request_control()['requests'] == 2
    cp.set_progress(progress)
    assert cp.read_request_control()['requests'] == 2
    assert cp.read_request_control()['max_requests'] == 8
    cp.close()


def test_budget_rejected_without_refund_or_revision_change(tmp_path):
    cp = Checkpoint(tmp_path / 'work.sqlite3')
    cp.reserve_request({}, 1)
    before = cp.read_request_control()
    with pytest.raises(ContractError, match='budget_exhausted'):
        cp.reserve_request({}, 10)
    assert cp.read_request_control() == before
    cp.close()


def test_unknown_version_changed_after_open_refuses_before_mutation(tmp_path):
    cp = Checkpoint(tmp_path / 'work.sqlite3')
    cp._connection.execute("UPDATE state SET value='3' WHERE key='checkpoint_format'")
    cp._connection.commit()
    statements = []
    cp._connection.set_trace_callback(statements.append)
    with pytest.raises(ContractError, match='unsupported_checkpoint_format'):
        cp.commit_page('new', [], {})
    assert not any(sql.startswith(('INSERT', 'UPDATE', 'CREATE', 'DELETE')) for sql in statements)
    cp.close()


def test_full_readers_ignore_invalid_derived_summary(tmp_path):
    from app.comment_export.checkpoint import read_checkpoint, read_control_snapshot
    cp = Checkpoint(tmp_path / 'work.sqlite3')
    cp.set_progress({'requests': 1, 'main_done': True})
    cp._connection.execute("UPDATE state SET value='not json' WHERE key='progress_summary'")
    cp._connection.commit()
    assert read_checkpoint(tmp_path)[2]['requests'] == 1
    assert read_control_snapshot(tmp_path)['progress']['requests'] == 1
    cp.close()
