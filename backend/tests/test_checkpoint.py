import sqlite3

import pytest

from app.comment_export.checkpoint import Checkpoint
from app.comment_export.contract import ContractError


def record(text='first', uid='10', root='1'):
    return {'comment_id': '2', 'root_id': root, 'author': {'uid': uid},
            'content': {'text': text}}


def test_empty_checkpoint_and_reopen(tmp_path):
    path = tmp_path / 'work.sqlite3'
    checkpoint = Checkpoint(path)
    assert checkpoint.freeze() == ([], {'coverage': {'status': 'partial'}})
    checkpoint.initialize({'video_id': 'bilibili:video:1', 'title': 'original'})
    checkpoint.commit_page('main:1', [record()], {'cursor': 'next'})
    checkpoint.close()
    reopened = Checkpoint(path)
    assert reopened.freeze()[0] == [record()]
    assert reopened.get_progress() == {'cursor': 'next', 'checkpoint_revision': 1}
    reopened.close()


def test_duplicate_page_never_rolls_back_progress(tmp_path):
    checkpoint = Checkpoint(tmp_path / 'work.sqlite3')
    checkpoint.commit_page('first', [record()], {'cursor': 1})
    checkpoint.commit_page('second', [record('latest')], {'cursor': 2})
    checkpoint.commit_page('first', [record('stale')], {'cursor': 1})
    assert checkpoint.get_progress() == {'cursor': 2, 'checkpoint_revision': 2}
    assert checkpoint.freeze()[0] == [record('latest')]
    checkpoint.close()


@pytest.mark.parametrize('changed', [record(uid='20'), record(root='3')])
def test_identity_conflict_rolls_back_whole_page(tmp_path, changed):
    checkpoint = Checkpoint(tmp_path / 'work.sqlite3')
    checkpoint.commit_page('first', [record()], {'cursor': 1})
    with pytest.raises(ContractError, match='identity_conflict'):
        checkpoint.commit_page('second', [changed], {'cursor': 2})
    assert checkpoint.get_progress() == {'cursor': 1, 'checkpoint_revision': 1}
    assert checkpoint.freeze()[0] == [record()]
    checkpoint.close()


def test_trigger_failure_rolls_back_page_comments_and_progress(tmp_path):
    path = tmp_path / 'work.sqlite3'
    checkpoint = Checkpoint(path)
    checkpoint.set_progress({'cursor': 0})
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON comments "
                           "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        checkpoint.commit_page('page', [record()], {'cursor': 1})
    assert checkpoint.freeze()[0] == []
    assert checkpoint.get_progress() == {'cursor': 0, 'checkpoint_revision': 0}
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 0
        connection.execute('DROP TRIGGER fail_insert')
    checkpoint.commit_page('page', [record()], {'cursor': 1})
    assert checkpoint.freeze()[0] == [record()]
    checkpoint.close()


def test_initialize_keeps_existing_metadata_and_rejects_other_video(tmp_path):
    checkpoint = Checkpoint(tmp_path / 'work.sqlite3')
    initial = {'video_id': 'bilibili:video:1', 'title': 'original'}
    checkpoint.initialize(initial)
    checkpoint.initialize({**initial, 'title': 'ignored'})
    assert checkpoint.freeze()[1] == initial
    with pytest.raises(ContractError, match='video_identity_mismatch'):
        checkpoint.initialize({'video_id': 'bilibili:video:2'})
    checkpoint.save_metadata({**initial, 'title': 'explicit'})
    assert checkpoint.freeze()[1]['title'] == 'explicit'
    checkpoint.close()


def test_metadata_and_progress_commit_together(tmp_path):
    checkpoint = Checkpoint(tmp_path / 'work.sqlite3')
    checkpoint.initialize({'video_id': 'bilibili:video:1', 'generation': 0})
    meta = {'video_id': 'bilibili:video:1', 'generation': 1}
    checkpoint.commit_page('page', [record()], {'metadata': meta, 'cursor': 1})
    assert checkpoint.freeze()[1] == meta
    checkpoint.close()


def test_metadata_failure_rolls_back_comment_page_and_progress(tmp_path):
    path = tmp_path / 'work.sqlite3'
    checkpoint = Checkpoint(path)
    original = {'video_id': 'bilibili:video:1', 'generation': 0}
    checkpoint.initialize(original)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_metadata BEFORE UPDATE ON state "
                           "WHEN NEW.key = 'metadata' BEGIN SELECT RAISE(ABORT, 'fail'); END")
    with pytest.raises(sqlite3.IntegrityError):
        checkpoint.commit_page('page', [record()], {
            'metadata': {**original, 'generation': 1}, 'cursor': 1})
    assert checkpoint.freeze() == ([], original)
    assert checkpoint.get_progress() == {}
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 0
    checkpoint.close()
