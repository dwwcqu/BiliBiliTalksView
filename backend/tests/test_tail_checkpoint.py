import sqlite3

import pytest

from app.comment_export.checkpoint import Checkpoint
from app.comment_export.contract import ContractError


def row(comment_id='2', uid='10', text='old'):
    return {'comment_id': comment_id, 'root_id': '1', 'author': {'uid': uid},
            'content': {'text': text}}


@pytest.fixture
def cp(tmp_path):
    checkpoint = Checkpoint(tmp_path / 'work.sqlite3')
    checkpoint.initialize({'video_id': 'video:1', 'count': 1})
    checkpoint.commit_page('initial', [row()], {'requests': 5, 'max_requests': 20})
    yield checkpoint
    checkpoint.close()


def test_candidates_isolated_and_survive_reopen(cp, tmp_path):
    before = cp.freeze()
    progress = {'requests': 6, 'metadata': {'video_id': 'video:1', 'count': 2}}
    attempt = cp.begin_tail('1', {'start_page': 19}, progress)
    payload = {'page': {'num': 19}, 'replies': [row('3')]}
    cp.stage_tail_page('1', attempt, 19, payload, progress)
    assert cp.freeze() == before
    assert cp.get_progress()['metadata'] == before[1]
    reopened = Checkpoint(tmp_path / 'work.sqlite3')
    assert reopened.read_tail_pages('1', attempt) == [payload]
    reopened.close()


def test_promotion_identity_failure_rolls_back_everything(cp):
    attempt = cp.begin_tail('1', {}, {'requests': 6})
    before, progress = cp.freeze(), cp.get_progress()
    with pytest.raises(ContractError, match='identity_conflict'):
        cp.promote_tail('1', attempt, [row('3'), row(uid='other')], {
            'requests': 7, 'metadata': {'video_id': 'video:1', 'count': 3}})
    assert cp.freeze() == before
    assert cp.get_progress() == progress
    cp.promote_tail('1', attempt, [row('3')], {'requests': 7})
    assert len(cp.freeze()[0]) == 2


def test_promote_is_atomic_and_duplicate_is_noop(cp, tmp_path):
    attempt = cp.begin_tail('1', {}, {'requests': 6})
    progress = {'requests': 7, 'metadata': {'video_id': 'video:1', 'count': 2}}
    cp.promote_tail('1', attempt, [row('3')], progress)
    assert cp.freeze() == ([row(), row('3')], progress['metadata'])
    assert progress['checkpoint_revision'] == 2
    before = cp.get_progress()
    cp.promote_tail('1', attempt, [row(uid='bad')], {'requests': 100})
    assert cp.get_progress() == before
    with sqlite3.connect(tmp_path / 'work.sqlite3') as connection:
        assert connection.execute('SELECT status FROM tail_attempts').fetchone() == ('promoted',)


def test_stage_idempotency_and_page_order(cp):
    attempt = cp.begin_tail('1', {}, {})
    cp.stage_tail_page('1', attempt, 20, {'b': 2, 'a': 1}, {'requests': 6})
    cp.stage_tail_page('1', attempt, 20, {'a': 1, 'b': 2}, {'requests': 7})
    cp.stage_tail_page('1', attempt, 19, {'first': True}, {'requests': 8})
    before = cp.get_progress()
    with pytest.raises(ContractError, match='tail_page_conflict'):
        cp.stage_tail_page('1', attempt, 20, {'changed': True}, {'requests': 9})
    assert cp.get_progress() == before
    assert cp.read_tail_pages('1', attempt) == [{'first': True}, {'a': 1, 'b': 2}]


@pytest.mark.parametrize('operation', ['stage', 'promote', 'abandon', 'read'])
def test_stale_attempt_cannot_write(cp, operation):
    old = cp.begin_tail('1', {}, {'requests': 6})
    new = cp.begin_tail('1', {}, {'requests': 7})
    assert new == old + 1
    before = cp.get_progress(), cp.freeze()
    with pytest.raises(ContractError, match='invalid_tail_attempt'):
        if operation == 'stage':
            cp.stage_tail_page('1', old, 1, {}, {'requests': 100})
        elif operation == 'promote':
            cp.promote_tail('1', old, [row('3')], {'requests': 100})
        elif operation == 'abandon':
            cp.abandon_tail('1', old, {'requests': 100})
        else:
            cp.read_tail_pages('1', old)
    assert (cp.get_progress(), cp.freeze()) == before


def test_abandon_keeps_budget_and_does_not_block_full_pages(cp):
    original = cp.freeze()
    progress = {'requests': 1, 'max_requests': 100, 'checkpoint_revision': 500}
    attempt = cp.begin_tail('1', {}, progress)
    cp.stage_tail_page('1', attempt, 1, {'replies': [row('3')]}, {'requests': 8})
    cp.abandon_tail('1', attempt, {'requests': 2, 'max_requests': 50, 'fallback': True})
    assert cp.freeze() == original
    assert cp.get_progress() == {'requests': 8, 'max_requests': 20, 'fallback': True,
                                 'checkpoint_revision': 1}
    cp.commit_page('reply:1:1:1', [row('4')], cp.get_progress())
    assert cp.freeze()[0] == [row(), row('4')]
    assert cp.begin_tail('1', {}, {}) == attempt + 1


def test_metadata_write_failure_rolls_back_promotion(cp, tmp_path):
    attempt = cp.begin_tail('1', {}, {})
    before = cp.freeze(), cp.get_progress()
    with sqlite3.connect(tmp_path / 'work.sqlite3') as connection:
        connection.execute("CREATE TRIGGER fail_metadata BEFORE UPDATE ON state "
                           "WHEN NEW.key = 'metadata' BEGIN SELECT RAISE(ABORT, 'fail'); END")
    with pytest.raises(sqlite3.IntegrityError):
        cp.promote_tail('1', attempt, [row('3')], {
            'metadata': {'video_id': 'video:1', 'count': 2}})
    assert (cp.freeze(), cp.get_progress()) == before
    with sqlite3.connect(tmp_path / 'work.sqlite3') as connection:
        assert connection.execute('SELECT status FROM tail_attempts').fetchone() == ('active',)


@pytest.mark.parametrize('operation', ['begin', 'stage', 'abandon'])
def test_tail_control_preserves_callers_metadata_reference(cp, operation):
    metadata = {'video_id': 'video:1', 'count': 2, '_threads': {'1': {'candidate': True}}}
    state = metadata['_threads']['1']
    progress = {'metadata': metadata, 'requests': 1, 'max_requests': 100}
    attempt = cp.begin_tail('1', {}, progress)
    if operation == 'stage':
        cp.stage_tail_page('1', attempt, 1, {}, progress)
    elif operation == 'abandon':
        cp.abandon_tail('1', attempt, progress)
    assert progress['metadata'] is metadata
    assert progress['metadata']['_threads']['1'] is state
    assert progress['requests'] == 5
    assert progress['max_requests'] == 20
    assert progress['checkpoint_revision'] == 1
    assert cp.freeze()[1] == {'video_id': 'video:1', 'count': 1}
    assert cp.get_progress()['metadata'] == cp.freeze()[1]


def test_confirmed_fallback_and_abandon_survive_reopen_together(cp, tmp_path):
    progress = {'metadata': cp.freeze()[1], 'requests': 6}
    attempt = cp.begin_tail('1', {}, progress)
    cp.stage_tail_page('1', attempt, 19, {'replies': [row('3')]}, progress)
    metadata = progress['metadata']
    metadata['_threads'] = {'1': {'tail_disabled': True, 'refresh_action': 'full'}}
    progress.update(requests=8, max_requests=10)
    cp.abandon_tail('1', attempt, progress, confirmed_progress=progress)
    reopened = Checkpoint(tmp_path / 'work.sqlite3')
    assert reopened.freeze() == ([row()], metadata)
    assert reopened.get_progress()['metadata'] == metadata
    assert reopened.get_progress()['requests'] == 8
    assert reopened.get_progress()['max_requests'] == 10
    assert reopened.get_progress()['checkpoint_revision'] == 1
    assert progress['metadata'] is metadata
    with sqlite3.connect(tmp_path / 'work.sqlite3') as connection:
        assert connection.execute('SELECT status FROM tail_attempts').fetchone() == ('abandoned',)
    reopened.close()


def test_confirmed_fallback_failure_rolls_back_attempt_and_metadata(cp, tmp_path):
    attempt = cp.begin_tail('1', {}, {'requests': 6})
    before = cp.freeze(), cp.get_progress()
    with sqlite3.connect(tmp_path / 'work.sqlite3') as connection:
        connection.execute("CREATE TRIGGER fail_fallback BEFORE UPDATE ON state "
                           "WHEN NEW.key = 'metadata' BEGIN SELECT RAISE(ABORT, 'fail'); END")
    with pytest.raises(sqlite3.IntegrityError):
        cp.abandon_tail('1', attempt, {'requests': 8}, confirmed_progress={
            'metadata': {'video_id': 'video:1', 'tail_disabled': True}})
    assert (cp.freeze(), cp.get_progress()) == before
    with sqlite3.connect(tmp_path / 'work.sqlite3') as connection:
        assert connection.execute('SELECT status FROM tail_attempts').fetchone() == ('active',)


def test_confirmed_fallback_preserves_strictest_budgets(cp):
    attempt = cp.begin_tail('1', {}, {})
    progress = {'requests': 9, 'max_requests': 12}
    cp.abandon_tail('1', attempt, progress, confirmed_progress={
        'requests': 7, 'max_requests': 15, 'metadata': cp.freeze()[1]})
    assert cp.get_progress()['requests'] == progress['requests'] == 9
    assert cp.get_progress()['max_requests'] == progress['max_requests'] == 12
