import json
import sqlite3

import pytest

from app.comment_export.checkpoint import Checkpoint, read_checkpoint
from app.comment_export.contract import ContractError


def test_v2_separates_metadata_and_request_control(tmp_path):
    path = tmp_path / 'work.sqlite3'
    cp = owned_checkpoint(path)
    metadata = {'video_id': '1', '_threads': {}, 'large': 'x' * 10000}
    cp.initialize(metadata)
    cp.set_progress({'metadata': metadata, 'requests': 2, 'max_requests': 10})
    statements = []
    cp._connection.set_trace_callback(statements.append)
    assert cp.reserve_request({'phase': 'main'}, 10)['requests'] == 3
    assert not any("'metadata'" in sql for sql in statements)
    cp.close()
    with sqlite3.connect(path) as connection:
        state = dict(connection.execute('SELECT key,value FROM state'))
    assert json.loads(state['checkpoint_format']) == 2
    assert 'metadata' not in json.loads(state['progress'])
    assert 'requests' not in json.loads(state['progress'])
    assert read_checkpoint(path)[2]['metadata'] == metadata


def test_unknown_version_rejected_without_writes(tmp_path):
    path = tmp_path / 'work.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE state (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        connection.execute("INSERT INTO state VALUES ('checkpoint_format','3')")
    before = path.read_bytes()
    with pytest.raises(ContractError):
        Checkpoint(path)
    assert path.read_bytes() == before

def legacy_checkpoint(path, embedded=None):
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE state (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        connection.execute('CREATE TABLE pages (key TEXT PRIMARY KEY)')
        connection.execute('CREATE TABLE comments (comment_id TEXT PRIMARY KEY,value TEXT NOT NULL)')
        progress = {'requests': 4, 'max_requests': 9, 'checkpoint_revision': 7}
        if embedded is not None:
            progress['metadata'] = embedded
        connection.executemany('INSERT INTO state VALUES (?,?)', [
            ('metadata', json.dumps({'video_id': '1', 'canonical': True})),
            ('progress', json.dumps(progress)),
        ])


def test_legacy_read_does_not_migrate_and_writer_uses_authority(tmp_path):
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path, {'video_id': '1', 'stale': True})
    before = path.read_bytes()
    assert read_checkpoint(path)[2]['metadata']['stale']
    assert path.read_bytes() == before
    cp = owned_checkpoint(path)
    assert cp.get_progress()['metadata'] == {'video_id': '1', 'canonical': True}
    assert cp.read_request_control() == {
        'requests': 4, 'max_requests': 9, 'checkpoint_revision': 7}
    cp.close()


def test_failed_migration_rolls_back_schema_and_state(tmp_path, monkeypatch):
    from app.comment_export import checkpoint_layout
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path)
    before = path.read_bytes()
    def fail(connection):
        raise RuntimeError('interrupted')
    monkeypatch.setattr(checkpoint_layout, 'rebuild_summary', fail)
    with pytest.raises(RuntimeError):
        owned_checkpoint(path)
    assert path.read_bytes() == before


def test_conflicting_embedded_identity_refuses_migration(tmp_path):
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path, {'video_id': '2'})
    before = path.read_bytes()
    with pytest.raises(ContractError, match='video_identity_mismatch'):
        owned_checkpoint(path)
    assert path.read_bytes() == before

@pytest.mark.parametrize('version', ['null', 'true', '"2"', '1', '3'])
def test_invalid_explicit_format_never_migrates(tmp_path, version):
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path)
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO state VALUES (?,?)', ('checkpoint_format', version))
    before = path.read_bytes()
    with pytest.raises(ContractError, match='unsupported_checkpoint_format'):
        Checkpoint(path)
    assert path.read_bytes() == before


def test_migration_preserves_records_pages_and_tail_candidates(tmp_path):
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path)
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO pages VALUES (?)', ('main:1',))
        connection.execute('INSERT INTO comments VALUES (?,?)', ('1', '{"comment_id":"1"}'))
        connection.execute('CREATE TABLE tail_attempts (root_id TEXT PRIMARY KEY, '
                           'attempt INTEGER,status TEXT,plan_json TEXT)')
        connection.execute('CREATE TABLE tail_pages (root_id TEXT,attempt INTEGER,page INTEGER,'
                           'payload TEXT,PRIMARY KEY(root_id,attempt,page))')
        connection.execute('INSERT INTO tail_attempts VALUES (?,?,?,?)', ('1', 3, 'active', '{}'))
        connection.execute('INSERT INTO tail_pages VALUES (?,?,?,?)', ('1', 3, 2, '{"rows":[]}'))
    cp = owned_checkpoint(path)
    assert cp.freeze()[0] == [{'comment_id': '1'}]
    assert cp.read_tail_pages('1', 3) == [{'rows': []}]
    assert cp._connection.execute('SELECT key FROM pages').fetchall() == [('main:1',)]
    cp.close()


def test_existing_incomplete_database_is_not_new_database(tmp_path):
    path = tmp_path / 'work.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE state (key TEXT PRIMARY KEY,value TEXT)')
    before = path.read_bytes()
    with pytest.raises(ContractError, match='invalid_checkpoint'):
        Checkpoint(path)
    assert path.read_bytes() == before


def test_legacy_migration_requires_current_lock_owner(tmp_path):
    from app.comment_export.publication import exclusive_lock
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path)
    before = path.read_bytes()
    with pytest.raises(ContractError, match='checkpoint_migration_requires_lock'):
        Checkpoint(path)
    assert path.read_bytes() == before
    with exclusive_lock(tmp_path / '.collect.lock'):
        cp = owned_checkpoint(path)
        cp.close()


def owned_checkpoint(path):
    from app.comment_export.publication import exclusive_lock, lock_owned
    if lock_owned(path.parent / '.collect.lock'):
        return Checkpoint(path)
    with exclusive_lock(path.parent / '.collect.lock'):
        return Checkpoint(path)


def test_other_thread_lock_ownership_cannot_authorize_migration(tmp_path):
    import threading

    from app.comment_export.publication import exclusive_lock
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path)
    ready, release = threading.Event(), threading.Event()
    def owner():
        with exclusive_lock(tmp_path / '.collect.lock'):
            ready.set()
            assert release.wait(10)
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ContractError, match='checkpoint_migration_requires_lock'):
            Checkpoint(path)
        with (
            pytest.raises(ContractError, match='busy'),
            exclusive_lock(tmp_path / '.collect.lock'),
        ):
            pass
    finally:
        release.set()
        thread.join(10)


def test_lock_remains_nonreentrant(tmp_path):
    from app.comment_export.publication import exclusive_lock, lock_owned
    lock = tmp_path / '.collect.lock'
    assert not lock_owned(lock)
    with exclusive_lock(lock):
        assert lock_owned(lock)
        with pytest.raises(ContractError, match='busy'), exclusive_lock(lock):
            pass
        assert lock_owned(lock)
    assert not lock_owned(lock)


def test_legacy_export_reads_without_migration(tmp_path, monkeypatch):
    from app.comment_export.cli import export_work
    path = tmp_path / 'work.sqlite3'
    legacy_checkpoint(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE state SET value=? WHERE key='metadata'", (
            json.dumps({'video_id': '1', 'source': {'aid': '1'}}),))
    before = path.read_bytes()
    def stop_after_read(*args):
        raise RuntimeError('export reached')
    monkeypatch.setattr('app.comment_export.cli.build_batch', stop_after_read)
    with pytest.raises(RuntimeError, match='export reached'):
        export_work(tmp_path, tmp_path / 'out')
    assert path.read_bytes() == before
