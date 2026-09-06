from copy import deepcopy

import pytest

from app.comment_export.zero_reply import (
    can_omit,
    merge_history,
    merge_observations,
    observe_zero,
)

STAMP = '2026-09-06T00:00:00Z'
SOURCE = {'oid': '1', 'comment_type': 1}


def raw_zero(**changes):
    return {'rpid_str': '100', 'oid_str': '1', 'root_str': '0', 'type': 1,
            'rcount': 0, 'count': 0, 'replies': [], 'member': {'mid': 42}, **changes}


def eligible(raw=None):
    return can_omit(observe_zero(raw or raw_zero(), SOURCE, STAMP),
                    merge_history(None, is_new=True, nonzero=False, unavailable=False),
                    stored_replies=0, policy='observe')


@pytest.mark.parametrize('value', [None, False, 0.0, '0', -1, 1])
def test_non_integer_or_nonzero_is_not_candidate(value):
    assert not eligible(raw_zero(rcount=value))


def test_missing_required_count_is_not_candidate():
    raw = raw_zero()
    del raw['rcount']
    assert not eligible(raw)


@pytest.mark.parametrize('preview', [[], None, 'missing'])
def test_valid_empty_preview_allows_observation_without_detail_proof(preview):
    raw = raw_zero(replies=preview)
    if preview == 'missing':
        del raw['replies']
    observation = observe_zero(raw, SOURCE, STAMP)
    assert eligible(raw)
    assert observation['root_identity'] == {
        'comment_id': '100', 'root_id': '100', 'author_uid': '42'}
    assert 'checked_now' not in observation.values()
    assert 'last_reply_checked_at' not in observation


@pytest.mark.parametrize('changes', [
    {'rpid_str': None}, {'rpid_str': '0'}, {'rpid_str': '01'}, {'rpid_str': True},
    {'rpid_str': '100', 'rpid': 101}, {'oid_str': '2'}, {'oid_str': None},
    {'oid_str': '1', 'oid': 2}, {'root_str': '100'}, {'root_str': None},
    {'root_str': '0', 'root': 100}, {'root_str': False}, {'type': True}, {'type': 2},
    {'type': '1'}, {'replies': [{}]}, {'replies': {}}, {'replies': ''},
    {'count': False}, {'count': None}, {'count': 0.0}, {'count': '0'}, {'count': 1},
])
def test_invalid_identity_counts_and_previews_reject_omission(changes):
    assert not eligible(raw_zero(**changes))


@pytest.mark.parametrize('field', ['oid_str', 'root_str', 'type'])
def test_missing_identity_is_not_success_evidence(field):
    raw = raw_zero()
    del raw[field]
    assert not eligible(raw)


def test_integer_identity_fallback_and_missing_auxiliary_count_are_valid():
    raw = raw_zero(rpid=100, oid=1, root=0)
    for field in ('rpid_str', 'oid_str', 'root_str', 'count'):
        del raw[field]
    assert eligible(raw)


def test_conflict_survives_zero_nonzero_zero_without_mutating_inputs():
    first = observe_zero(raw_zero(), SOURCE, STAMP)
    bad = observe_zero(raw_zero(rcount=1), SOURCE, STAMP)
    original = deepcopy(first)
    merged = merge_observations(merge_observations(first, bad), first)
    assert first == original
    assert merged['conflict'] is True
    assert merged['nonzero_observed'] is True
    assert not can_omit(merged, merge_history(None, is_new=True, nonzero=False,
                                             unavailable=False),
                        stored_replies=0, policy='observe')


def test_duplicate_zero_remains_eligible_and_different_root_conflicts():
    first = observe_zero(raw_zero(), SOURCE, STAMP)
    merged = merge_observations(first, first)
    assert eligible()
    assert merged == first
    assert merge_observations(first, observe_zero(raw_zero(rpid_str='101'), SOURCE,
                                                   STAMP))['conflict'] is True


@pytest.mark.parametrize('previous', [None, {}, {'version': 2},
    {'version': 1, 'known': True, 'ever_nonzero_observed': False},
    {'version': True, 'known': True, 'ever_nonzero_observed': False,
     'ever_source_unavailable': False}])
def test_old_or_malformed_history_stays_unknown(previous):
    history = merge_history(previous, is_new=False, nonzero=False, unavailable=False)
    assert history['known'] is False
    history = merge_history(history, is_new=True, nonzero=False, unavailable=False)
    assert history['known'] is False
    assert not can_omit(observe_zero(raw_zero(), SOURCE, STAMP), history,
                        stored_replies=0, policy='observe')


def test_history_flags_are_monotonic_even_with_unknown_version():
    history = merge_history(None, is_new=True, nonzero=True, unavailable=False)
    original = deepcopy(history)
    updated = merge_history(history, is_new=False, nonzero=False, unavailable=True)
    assert history == original
    assert updated == {'version': 1, 'known': True, 'ever_nonzero_observed': True,
                       'ever_source_unavailable': True}
    updated['version'] = 99
    merged = merge_history(updated, is_new=True, nonzero=False, unavailable=False)
    assert merged['known'] is False
    assert merged['ever_nonzero_observed'] is True
    assert merged['ever_source_unavailable'] is True


@pytest.mark.parametrize('stored,policy,nonzero,unavailable', [
    (1, 'observe', False, False), (False, 'observe', False, False),
    (0, 'verify', False, False), (0, 'observe', True, False),
    (0, 'observe', False, True),
])
def test_saved_replies_policy_or_history_counterevidence_prevent_omission(
        stored, policy, nonzero, unavailable):
    history = merge_history(None, is_new=True, nonzero=nonzero, unavailable=unavailable)
    assert not can_omit(observe_zero(raw_zero(), SOURCE, STAMP), history,
                        stored_replies=stored, policy=policy)


@pytest.mark.parametrize('changes', [{'rcount': 1}, {'count': 1}, {'replies': [{}]}])
def test_positive_counts_or_real_preview_provide_history_counterevidence(changes):
    assert observe_zero(raw_zero(**changes), SOURCE, STAMP)['nonzero_observed'] is True


@pytest.mark.parametrize('stamp', ['', 'bad', '2026-09-06T00:00:00'])
def test_invalid_observation_timestamp_is_not_evidence(stamp):
    history = merge_history(None, is_new=True, nonzero=False, unavailable=False)
    assert not can_omit(observe_zero(raw_zero(), SOURCE, stamp), history,
                        stored_replies=0, policy='observe')


def evidence_inputs():
    observation = observe_zero(raw_zero(), SOURCE, STAMP)
    history = merge_history(None, is_new=True, nonzero=False, unavailable=False)
    row = {'video_id': 'bilibili:video:1', 'comment_id': '100', 'root_id': '100',
           'kind': 'root', 'author': {'uid': '42'}}
    return observation, history, row


def test_evidence_builder_and_validator_bind_current_root_and_work():
    from app.comment_export.zero_reply import build_zero_evidence, validate_zero_evidence

    observation, history, row = evidence_inputs()
    evidence = build_zero_evidence(observation, history, row, 'work-1')
    assert evidence['work_id'] == 'work-1'
    evidence['unexpected'] = 'must not persist'
    validated = validate_zero_evidence(
        evidence, work_id='work-1', video_id='bilibili:video:1', root_row=row,
        history=history, stored_replies=0, observed_main_ids=['100'],
        snapshot_at='2026-09-06T01:00:00Z', started_at=STAMP)
    assert validated is not None
    assert 'unexpected' not in validated
    assert 'unexpected' in evidence


@pytest.mark.parametrize('changes', [
    {'work_id': 'other'}, {'video_id': 'bilibili:video:2'}, {'stored_replies': 1},
    {'observed_main_ids': []}, {'snapshot_at': '2026-09-05T23:00:00Z'},
    {'started_at': '2026-09-06T00:01:00Z'}, {'snapshot_at': 'invalid'},
    {'history': None}, {'root_row': {'comment_id': '101'}},
])
def test_evidence_rejects_other_work_video_missing_observation_and_time(changes):
    from app.comment_export.zero_reply import build_zero_evidence, validate_zero_evidence

    observation, history, row = evidence_inputs()
    evidence = build_zero_evidence(observation, history, row, 'work-1')
    arguments = {'work_id': 'work-1', 'video_id': 'bilibili:video:1', 'root_row': row,
                 'history': history, 'stored_replies': 0, 'observed_main_ids': ['100'],
                 'snapshot_at': '2026-09-06T01:00:00Z', 'started_at': STAMP}
    arguments.update(changes)
    assert validate_zero_evidence(evidence, **arguments) is None


def test_detail_reply_after_main_observation_invalidates_evidence():
    from app.comment_export.zero_reply import build_zero_evidence

    observation, history, row = evidence_inputs()
    history = merge_history(history, is_new=False, nonzero=True, unavailable=False)
    assert build_zero_evidence(observation, history, row, 'work-1') is None


@pytest.mark.parametrize('changes', [{'author': {'uid': '43'}}, {'kind': 'reply'},
                                     {'video_id': 'bilibili:video:2'}])
def test_builder_rejects_changed_root_identity(changes):
    from app.comment_export.zero_reply import build_zero_evidence

    observation, history, row = evidence_inputs()
    assert build_zero_evidence(observation, history, dict(row, **changes), 'work-1') is None
