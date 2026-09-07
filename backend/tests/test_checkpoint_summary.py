import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.comment_export.checkpoint import Checkpoint, read_progress_summary
from app.comment_export.contract import ContractError
from app.jobs import ownership


@pytest.fixture
def checkpoint(tmp_path):
    cp = Checkpoint(tmp_path / 'work.sqlite3')
    cp.initialize({'video_id': 'video:1', '_threads': {
        '1': {'reply_check_state': 'complete'}, '2': {'unavailable': True}},
        'large_marker': 'UNPARSED_METADATA' * 10000})
    cp.set_progress({'requests': 7, 'max_requests': 20})
    yield cp
    cp.close()


def lease_for(monkeypatch, tmp_path):
    statements = []

    @contextmanager
    def begin():
        yield SimpleNamespace(execute=statements.append)

    monkeypatch.setattr(ownership, 'guard_task', lambda *args, **kwargs: {})
    owner = SimpleNamespace(engine=SimpleNamespace(begin=begin), token='owner', pid=1)
    lease = ownership.TaskLease(owner, {'kind': 'job', 'id': 'job'})
    lease.work_dir = tmp_path
    return lease, statements


@pytest.mark.parametrize('progress,phase', [({}, 'main'), ({'main_done': True}, 'replies'),
                                         ({'finished': True}, 'import')])
def test_worker_publishes_summary_without_parsing_metadata(
    checkpoint, tmp_path, monkeypatch, progress, phase
):
    checkpoint.set_progress(progress)
    # Corrupt only the large display-independent metadata after the summary is committed.
    with sqlite3.connect(tmp_path / 'work.sqlite3') as conn:
        conn.execute("UPDATE state SET value='not JSON' WHERE key='metadata'")
    lease, statements = lease_for(monkeypatch, tmp_path)
    lease.renew()
    values = statements[-1].compile().params
    assert values['phase'] == phase
    assert values['progress'] == {
        'phase': phase, 'requests': 7, 'comments': 0, 'threads': 2,
        'verified_threads': 1, 'unavailable_threads': 1,
        'updated_at': values['progress']['updated_at'],
    }
    assert values['lease_until'] > values['heartbeat_at']


@pytest.mark.parametrize('broken', [None, 'not JSON', '{}', '{"comments": -1}'])
def test_unavailable_summary_renews_without_refreshing_display(
    checkpoint, tmp_path, monkeypatch, broken
):
    with sqlite3.connect(tmp_path / 'work.sqlite3') as conn:
        conn.execute("DELETE FROM state WHERE key='progress_summary'")
        if broken is not None:
            conn.execute('INSERT INTO state VALUES (?, ?)', ('progress_summary', broken))
    lease, statements = lease_for(monkeypatch, tmp_path)
    lease.renew()
    values = statements[-1].compile().params
    assert 'progress' not in values
    assert 'phase' not in values
    assert values['lease_until'] > values['heartbeat_at']


def test_invalid_control_still_stops_renewal(checkpoint, tmp_path, monkeypatch):
    with sqlite3.connect(tmp_path / 'work.sqlite3') as conn:
        conn.execute("UPDATE state SET value=? WHERE key='request_control'",
                     ('{"requests": -1}',))
    lease, statements = lease_for(monkeypatch, tmp_path)
    with pytest.raises(ContractError, match='invalid_control_state'):
        lease.renew()
    assert len(statements) == 1  # statement timeout only; no lease or display update.


def test_tail_candidates_are_excluded_until_promotion(checkpoint, tmp_path):
    row = {'comment_id': '3', 'root_id': '1', 'author': {'uid': '10'},
           'content': {'text': 'candidate'}}
    attempt = checkpoint.begin_tail('1', {}, {})
    checkpoint.stage_tail_page('1', attempt, 1, {'replies': [row]}, {})
    assert read_progress_summary(tmp_path)['summary']['comments'] == 0
    checkpoint.promote_tail('1', attempt, [row], {})
    assert read_progress_summary(tmp_path)['summary']['comments'] == 1


def test_reader_parsed_bytes_do_not_grow_with_metadata(checkpoint, tmp_path, monkeypatch):
    from app.comment_export import checkpoint as module
    from app.comment_export import checkpoint_layout as layout

    parsed = []
    original = layout.parse_json

    def track(value):
        parsed.append(len(value))
        assert 'UNPARSED_METADATA' not in value
        return original(value)

    monkeypatch.setattr(layout, 'parse_json', track)
    monkeypatch.setattr(module, 'parse_json', track)
    first = read_progress_summary(tmp_path)
    first_bytes = sum(parsed)
    parsed.clear()
    with sqlite3.connect(tmp_path / 'work.sqlite3') as conn:
        conn.execute("UPDATE state SET value=? WHERE key='metadata'",
                     ('{"large_marker":"' + 'UNPARSED_METADATA' * 100000 + '"}',))
    assert read_progress_summary(tmp_path) == first
    assert sum(parsed) == first_bytes
    assert first_bytes < 2000


def test_reader_control_and_summary_share_snapshot(checkpoint, tmp_path, monkeypatch):
    import json

    from app.comment_export import checkpoint_layout as layout

    database = tmp_path / 'work.sqlite3'
    with sqlite3.connect(database) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    original = layout.parse_json
    wrote = False

    def interleave(value):
        nonlocal wrote
        parsed = original(value)
        if isinstance(parsed, dict) and parsed.get('requests') == 7 and not wrote:
            wrote = True
            with sqlite3.connect(database) as writer:
                writer.execute("UPDATE state SET value=? WHERE key='request_control'",
                               (json.dumps({'requests': 8, 'max_requests': 20}),))
                writer.execute("UPDATE state SET value=? WHERE key='progress_summary'",
                               (json.dumps({'comments': 1, 'threads': 2, 'verified_threads': 1,
                                            'unavailable_threads': 1, 'phase': 'replies'}),))
        return parsed

    monkeypatch.setattr(layout, 'parse_json', interleave)
    old = read_progress_summary(tmp_path)
    assert wrote
    assert old['request_control']['requests'] == 7
    assert old['summary']['comments'] == 0
    assert old['summary']['phase'] == 'main'
    new = read_progress_summary(tmp_path)
    assert new['request_control']['requests'] == 8
    assert new['summary']['comments'] == 1
    assert new['summary']['phase'] == 'replies'


@pytest.mark.parametrize('local_cap,current_cap,expected', [(20, 10, 10), (10, 20, 10)])
def test_prepare_job_reconciles_budget_without_increase(
    tmp_path, monkeypatch, local_cap, current_cap, expected
):
    from app.jobs import work

    collection = tmp_path / 'collection'
    cp = Checkpoint(collection / 'work.sqlite3')
    cp.set_progress({'job_id': 'job', 'baseline_version': 0, 'base_state_id': None,
                     'effective_mode': 'full', 'requests': 3, 'max_requests': local_cap})
    cp.close()
    current = {'baseline_version': 0, 'requests': 5, 'max_requests': current_cap}
    monkeypatch.setattr(work, 'guard_task', lambda *args, **kwargs: current)
    lease, _ = lease_for(monkeypatch, tmp_path)
    assert work.prepare_job(lease.owner, lease.claim, tmp_path) == (collection, 0)
    snapshot = read_progress_summary(collection)
    assert snapshot['request_control']['requests'] == 5
    assert snapshot['request_control']['max_requests'] == expected


def test_summary_connection_failure_marks_heartbeat_failure(checkpoint, tmp_path, monkeypatch):
    from app.comment_export import checkpoint as module

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError('cannot open')

    monkeypatch.setattr(module.sqlite3, 'connect', fail_connect)
    with pytest.raises(ContractError, match='invalid_control_state'):
        read_progress_summary(tmp_path)
    lease, statements = lease_for(monkeypatch, tmp_path)
    lease.stop = SimpleNamespace(wait=lambda _: False)
    lease._heartbeat()
    assert lease.failure == 'lease_lost'
    assert len(statements) == 1
