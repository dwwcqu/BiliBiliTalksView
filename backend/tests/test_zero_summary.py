"""Optional zero observation summaries never change legacy read semantics."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.api.discussions import summary


def saved_row():
    from app.comment_export.zero_schedule import decide_policy

    work_id = str(uuid4())
    snapshot = decide_policy('auto', '2026-09-06T00:00:00Z', 24, None,
                             work_id=work_id, baseline_binding=None)
    return {
        'state_id': str(uuid4()), 'video_id': 'bilibili:video:1', 'title': 'sample',
        'coverage': {'status': 'partial', 'reasons': ['replies_incomplete']},
        'source_metadata': {'manifest': {'counts': {'comments': 2, 'root_comments': 2}}},
        'hour_bucket': '2026-09-06T00:00:00Z',
        'captured_from': '2026-09-06T00:00:00Z', 'captured_to': '2026-09-06T00:01:00Z',
        'lifecycle': 'partial',
        'refresh_context': {'version': 1, 'video_id': 'bilibili:video:1', 'job_id': work_id,
            'evidence': {'refresh': {'zero_policy_snapshot': snapshot}},
            'zero_reply_summary': {'version': 1, 'work_id': work_id, 'observed_threads': 1}},
    }


def test_zero_count_is_state_scoped_and_optional():
    current = saved_row()
    current['refresh_context']['zero_reply_summary']['observed_threads'] = 0
    partial = saved_row()
    assert summary(current)['zero_reply_observed_threads'] == 0
    assert summary(partial)['zero_reply_observed_threads'] == 1
    legacy = deepcopy(current)
    legacy['refresh_context'] = None
    assert 'zero_reply_observed_threads' not in summary(legacy)


@pytest.mark.parametrize('value', [True, -1, 0.5, '1', 3, None])
def test_malformed_count_is_not_exposed(value):
    row = saved_row()
    row['refresh_context']['zero_reply_summary']['observed_threads'] = value
    assert 'zero_reply_observed_threads' not in summary(row)


@pytest.mark.parametrize('corruption', ['work', 'video', 'version', 'verified'])
def test_summary_binding_and_coverage_are_checked(corruption):
    row = saved_row()
    if corruption == 'work':
        row['refresh_context']['zero_reply_summary']['work_id'] = str(uuid4())
    elif corruption == 'video':
        row['refresh_context']['video_id'] = 'bilibili:video:2'
    elif corruption == 'version':
        row['refresh_context']['zero_reply_summary']['version'] = 99
    else:
        row['coverage']['status'] = 'verified'
    assert 'zero_reply_observed_threads' not in summary(row)


@pytest.mark.parametrize('change', ['missing_snapshot_fields', 'different_job', 'future'])
def test_unrecognizable_snapshot_does_not_expose_count(change):
    row = saved_row()
    context = row['refresh_context']
    snapshot = context['evidence']['refresh']['zero_policy_snapshot']
    if change == 'missing_snapshot_fields':
        context['evidence']['refresh']['zero_policy_snapshot'] = {
            'version': 1, 'work_id': snapshot['work_id']}
    elif change == 'different_job':
        context['job_id'] = str(uuid4())
    else:
        row['captured_to'] = '2025-01-01T00:00:00Z'
    assert 'zero_reply_observed_threads' not in summary(row)
