from copy import deepcopy

import pytest

from app.comment_export.zero_schedule import complete_schedule, decide_policy, validate_schedule

START = '2026-09-06T00:00:00+00:00'
LATER = '2026-09-06T20:00:00+00:00'
DUE = '2026-09-07T00:00:00+00:00'


def test_policy_first_auto_and_untrusted_old_baseline():
    first = decide_policy('auto', START, 24, None, work_id='a', baseline_binding=None)
    assert (first['range_mode'], first['zero_policy']) == ('full', 'observe')
    assert first['verify_due_at'] == DUE
    for baseline, mode in [({}, 'auto'), (None, 'full')]:
        result = decide_policy(mode, START, 24, baseline, work_id='b', baseline_binding='old')
        assert (result['range_mode'], result['zero_policy']) == ('full', 'verify')


def fixture():
    snapshot = decide_policy('full', START, 24, None, work_id='a', baseline_binding=None)
    rows = [{'kind': 'root', 'comment_id': '100', 'root_id': '100', 'parent_id': None,
             'video_id': 'BV1', 'author': {'uid': '5'}, 'content': {'message': 'hello'}}]
    from app.comment_export.incremental import root_signature
    metadata = {'video_id': 'BV1', 'captured_to': START, '_refresh': {
        'zero_policy_snapshot': snapshot, 'baseline_digest': None,
        'observed_main_ids': ['100'], 'observed_ids': ['100']}, '_threads': {'100': {
        'pagination_status': 'verified', 'reply_check_state': 'complete', 'count': 0,
        'checked_count': 0, 'checked_root': root_signature(rows[0]),
        'reply_verification': 'checked_now', 'last_reply_checked_at': START,
        'last_complete_at': START}}}
    return snapshot, metadata, rows


def test_complete_and_binding_survives_export_uuid_change():
    snapshot, metadata, rows = fixture()
    proof = complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True})
    assert proof is not None
    assert validate_schedule(proof, metadata, rows) == proof
    rows[0].update(export_id='new-export', schema_version='2.0.0')
    metadata['export_id'] = 'new-export'
    assert validate_schedule(proof, metadata, rows) == proof
    rows[0]['content']['message'] = 'changed'
    assert validate_schedule(proof, metadata, rows) is None


@pytest.mark.parametrize('mutation', ['root', 'video', 'work', 'time', 'unfinished'])
def test_completed_proof_rejects_changed_context(mutation):
    snapshot, metadata, rows = fixture()
    proof = complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True})
    if mutation == 'root':
        metadata['_threads']['200'] = deepcopy(metadata['_threads']['100'])
    elif mutation == 'video':
        metadata['video_id'] = 'BV2'
    elif mutation == 'work':
        metadata['_refresh']['zero_policy_snapshot']['work_id'] = 'another'
    elif mutation == 'time':
        proof['scan_completed_at'] = '2099-01-01T00:00:00+00:00'
    else:
        metadata['_threads']['100']['reply_verification'] = 'not_checked'
    assert validate_schedule(proof, metadata, rows) is None


def test_finished_flag_is_insufficient():
    snapshot, metadata, rows = fixture()
    assert complete_schedule(snapshot, metadata, rows, {'finished': True}) is None
    metadata['_threads']['100']['last_reply_checked_at'] = '2026-09-05T23:00:00+00:00'
    assert complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True}) is None


def test_deadline_inherited_and_only_shortened():
    snapshot, metadata, rows = fixture()
    metadata['_refresh']['zero_reply_schedule'] = complete_schedule(
        snapshot, metadata, rows, {'finished': True, 'main_done': True})
    metadata['_zero_schedule_rows'] = rows
    for interval, expected in [(24, DUE), (48, DUE), (12, '2026-09-06T12:00:00+00:00')]:
        result = decide_policy('auto', LATER, interval, metadata, work_id='b', baseline_binding='x')
        assert result['verify_due_at'] == expected
        assert result['zero_policy'] == ('verify' if interval == 12 else 'observe')
    assert decide_policy('auto', DUE, 24, metadata, work_id='c', baseline_binding='x')[
        'zero_policy'] == 'verify'


def test_fractional_interval_compatible_and_invalid_interval_rejected():
    assert decide_policy('auto', START, .5, None, work_id='a', baseline_binding=None)[
        'verify_due_at'] == '2026-09-06T00:30:00+00:00'
    for value in [True, 0, -1, float('nan'), float('inf')]:
        with pytest.raises(ValueError):
            decide_policy('auto', START, value, None, work_id='a', baseline_binding=None)


def test_observed_skip_requires_matching_current_main_count():
    snapshot, metadata, rows = fixture()
    snapshot.update(range_mode='incremental', zero_policy='observe', requested_mode='auto')
    state = metadata['_threads']['100']
    state.update(skip_refresh=True, reply_verification='reused_unverified',
                 pagination_status='partial', main_count=5)
    assert complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True}) is None


def test_legacy_full_deadline_and_future_timestamp():
    metadata = {'captured_to': START, '_refresh': {
        'last_full_scan_completed_at': START, 'full_scan_incomplete': False}}
    assert decide_policy('auto', LATER, 24, metadata, work_id='b', baseline_binding='x')[
        'zero_policy'] == 'observe'
    metadata['_refresh']['last_full_scan_completed_at'] = DUE
    assert decide_policy('auto', LATER, 24, metadata, work_id='b', baseline_binding='x')[
        'zero_policy'] == 'verify'


@pytest.mark.parametrize('stopping', [{'blocked': True}, {'stopped_reason': 'max_requests'},
                                    {'finished': False}])
def test_stopped_work_never_builds_schedule(stopping):
    snapshot, metadata, rows = fixture()
    progress = {'finished': True, 'main_done': True, **stopping}
    assert complete_schedule(snapshot, metadata, rows, progress) is None


def zero_fixture():
    from app.comment_export.zero_reply import build_zero_evidence, merge_history, observe_zero
    snapshot, metadata, rows = fixture()
    snapshot.update(requested_mode='auto', zero_policy='observe')
    metadata['video_id'] = rows[0]['video_id'] = 'bilibili:video:1'
    state = metadata['_threads']['100']
    state.clear()
    history = merge_history(None, is_new=True, nonzero=False, unavailable=False)
    observation = observe_zero({'rpid_str': '100', 'oid_str': '1', 'root_str': '0',
        'type': 1, 'rcount': 0, 'member': {'mid': 5}}, {'oid': '1', 'comment_type': 1}, START)
    state.update(zero_completed=True, pagination_status='partial', count=0,
        reply_verification='not_checked', reply_check_state='needs_check',
        zero_reply_history=history,
        zero_reply_evidence=build_zero_evidence(observation, history, rows[0], 'a'))
    return snapshot, metadata, rows


def test_zero_completion_preserves_initial_deadline_across_late_resume():
    snapshot, metadata, rows = zero_fixture()
    metadata['captured_to'] = '2026-09-07T01:00:00+00:00'
    proof = complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True})
    assert proof is not None
    assert proof['verify_due_at'] == DUE
    assert proof['verification_anchor_at'] == START
    assert proof['full_details_completed'] is False
    metadata['_refresh']['zero_reply_schedule'] = proof
    metadata['_zero_schedule_rows'] = rows
    result = decide_policy('auto', metadata['captured_to'], 24, metadata,
                           work_id='b', baseline_binding='x')
    assert result['zero_policy'] == 'verify'
    assert validate_schedule(proof, metadata, rows) == proof


@pytest.mark.parametrize('damage', ['absent', 'reply', 'history', 'wrong_work', 'verify'])
def test_zero_completion_requires_current_eligible_observation(damage):
    snapshot, metadata, rows = zero_fixture()
    state = metadata['_threads']['100']
    if damage == 'absent':
        metadata['_refresh']['observed_main_ids'] = []
    elif damage == 'reply':
        rows.append(dict(rows[0], kind='reply', comment_id='101', parent_id='100'))
    elif damage == 'history':
        state['zero_reply_history']['ever_nonzero_observed'] = True
    elif damage == 'wrong_work':
        state['zero_reply_evidence']['work_id'] = 'old'
    else:
        snapshot['zero_policy'] = 'verify'
    assert complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True}) is None


def test_successful_detail_scan_restarts_deadline_at_completion():
    snapshot, metadata, rows = fixture()
    metadata['captured_to'] = LATER
    metadata['_threads']['100'].update(last_reply_checked_at=LATER, last_complete_at=LATER)
    proof = complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True})
    assert proof['full_details_completed'] is True
    assert proof['verification_anchor_at'] == LATER
    assert proof['verify_due_at'] == '2026-09-07T20:00:00+00:00'


def test_compact_zero_completion_needs_no_temporary_flags_or_raw_observation():
    snapshot, metadata, rows = zero_fixture()
    proof = complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True})
    metadata['_threads']['100'].pop('zero_completed')
    metadata['_refresh'].pop('observed_main_ids')
    assert validate_schedule(proof, metadata, rows) == proof


def test_detail_completion_cannot_claim_replies_missing_from_records():
    snapshot, metadata, rows = fixture()
    metadata['_threads']['100'].update(count=1, checked_count=1)
    assert complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True}) is None


@pytest.mark.parametrize('damage', ['explicit_full', 'expired_at_start'])
def test_inconsistent_frozen_observe_snapshot_rejected(damage):
    snapshot, metadata, rows = zero_fixture()
    if damage == 'explicit_full':
        snapshot['requested_mode'] = 'full'
    else:
        snapshot['started_at'] = DUE
        metadata['captured_to'] = DUE
        metadata['_threads']['100']['zero_reply_evidence']['observed_at'] = DUE
    assert complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True}) is None


def test_old_reply_cannot_cover_missing_current_detail_observation():
    snapshot, metadata, rows = fixture()
    metadata['_threads']['100'].update(count=1, checked_count=1)
    rows.append(dict(rows[0], kind='reply', comment_id='101', parent_id='100'))
    assert complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True}) is None


def test_snapshot_whitelist_and_invalid_mode():
    from app.comment_export.zero_schedule import validate_policy_snapshot
    snapshot, _, _ = fixture()
    assert validate_policy_snapshot(dict(snapshot, unknown='discard')) == snapshot
    assert validate_policy_snapshot(dict(snapshot, zero_policy='observe')) is None
    assert validate_policy_snapshot(dict(snapshot, version=True)) is None


def test_schedule_rejects_different_current_baseline_binding():
    snapshot, metadata, rows = fixture()
    metadata['_refresh']['baseline_digest'] = 'different-baseline'
    assert complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True}) is None


@pytest.mark.parametrize('zero', [False, True])
def test_completion_indexes_replies_once_for_many_roots(zero):
    snapshot, metadata, template = zero_fixture() if zero else fixture()
    root_lookups = 0

    class CountedRow(dict):
        def get(self, key, default=None):
            nonlocal root_lookups
            if key == 'root_id':
                root_lookups += 1
            return super().get(key, default)

    rows = [CountedRow(template[0], comment_id=str(root), root_id=str(root))
            for root in range(100, 200)]
    state = metadata['_threads']['100']
    metadata['_threads'] = {row['root_id']: deepcopy(state) for row in rows}
    if zero:
        for root, item in metadata['_threads'].items():
            item['zero_reply_evidence']['root_id'] = root
            item['zero_reply_evidence']['root_identity'].update(comment_id=root, root_id=root)
    metadata['_refresh']['observed_ids'] = [row['root_id'] for row in rows]
    metadata['_refresh']['observed_main_ids'] = list(metadata['_threads'])
    proof = complete_schedule(snapshot, metadata, rows, {'finished': True, 'main_done': True})
    assert proof is not None
    assert validate_schedule(proof, metadata, rows) == proof
    assert root_lookups < len(rows) * 10
