from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.comment_export.tail import (
    TailMismatch,
    build_tail_evidence,
    select_tail_plan,
    tail_start_page,
    validate_tail_evidence,
    validate_tail_pages,
)

STAMP = '2026-09-06T08:00:00+00:00'
LATER = '2026-09-06T09:00:00+00:00'


def root():
    return {'comment_id': '100', 'root_id': '100', 'kind': 'root', 'parent_id': None,
            'author': {'uid': '1'}, 'content': {'message': 'root'}}


def replies(count):
    return [{'comment_id': str(9007199254741999 - i), 'root_id': '100', 'kind': 'reply',
             'parent_id': '100', 'author': {'uid': '2'}, 'content': {'message': str(i)},
             'created_at': (datetime(2026, 1, 1, tzinfo=UTC)
                            + timedelta(seconds=i)).isoformat(), 'collected_at': STAMP}
            for i in range(count)]


def baseline(count=397):
    rows = replies(count)
    evidence = build_tail_evidence(rows, root(), count, STAMP)
    old = {row['comment_id']: row for row in [root(), *rows]}
    state = {'tail_evidence': evidence, 'main_core': root(), 'main_observed': True, 'main_count': count + 1,
             'reply_check_state': 'complete', 'count': count, 'last_complete_at': STAMP}
    return old, state


def pages(plan, rows):
    return [{'page': {'num': page, 'size': 20, 'count': len(rows)}, 'root': root(),
             'replies': deepcopy(rows[(page - 1) * 20:page * 20])}
            for page in range(plan.start_page, plan.last_page + 1)]


@pytest.mark.parametrize('count,expected', [(61, 3), (397, 19), (400, 19)])
def test_overlap_start(count, expected):
    assert tail_start_page(count) == expected


@pytest.mark.parametrize('count,new,last', [(397, 398, 20), (400, 401, 21)])
def test_plan_and_append(count, new, last):
    old, state = baseline(count)
    plan = select_tail_plan(state, root(), old, 'incremental', new)
    assert (plan.start_page, plan.last_page) == (19, last)
    rows, evidence = validate_tail_pages(plan, pages(plan, replies(new)), old, root(),
                                         LATER, state['tail_evidence'])
    assert rows == replies(new)[360:]
    assert evidence['last_full_checked_at'] == STAMP
    assert evidence['latest_tail_checked_at'] == LATER
    assert evidence['source_count'] == new
    old.update({row['comment_id']: row for row in rows})
    assert validate_tail_evidence(evidence, root(), old, snapshot_at=LATER) == evidence


@pytest.mark.parametrize('change', ['version', 'duplicate', 'cross_root', 'future', 'naive',
                                    'offset', 'unknown_uid', 'count', 'signature'])
def test_bad_evidence(change):
    old, state = baseline()
    value = state['tail_evidence']
    if change == 'version':
        value['version'] = 2
    elif change == 'duplicate':
        value['anchor_ids'][1] = value['anchor_ids'][0]
    elif change == 'cross_root':
        old[value['anchor_ids'][0]]['root_id'] = '200'
    elif change == 'future':
        value['latest_tail_checked_at'] = LATER
    elif change == 'naive':
        value['last_full_checked_at'] = '2026-09-06T08:00:00'
    elif change == 'offset':
        value['anchor_start_page'] = 18
    elif change == 'unknown_uid':
        old[value['anchor_ids'][0]]['author']['uid'] = None
    elif change == 'count':
        value['source_count'] = True
    else:
        value['root_signature'] = 'changed'
    assert validate_tail_evidence(value, root(), old, snapshot_at=STAMP) is None


@pytest.mark.parametrize('change', ['small', 'full', 'same', 'body', 'absent', 'unavailable',
                                    'extra', 'unknown_uid'])
def test_selection_rejects(change):
    old, state = baseline(60 if change == 'small' else 397)
    row, mode, new = root(), 'incremental', state['count'] + 1
    if change == 'full':
        mode = 'full'
    elif change == 'same':
        new -= 1
    elif change == 'body':
        row['content']['message'] = 'changed'
    elif change == 'absent':
        state.pop('main_core')
    elif change == 'unavailable':
        state['unavailable'] = {'reason': 'source_reply_unavailable'}
    elif change == 'extra':
        old['999'] = dict(replies(1)[0], comment_id='999')
    elif change == 'unknown_uid':
        old[next(key for key in old if key != '100')]['author']['uid'] = None
    assert select_tail_plan(state, row, old, mode, new) is None


@pytest.mark.parametrize('change', ['count', 'page', 'size', 'empty', 'order', 'duplicate',
                                    'cross_root', 'author', 'parent', 'root', 'known_new'])
def test_tail_mismatch(change):
    old, state = baseline()
    plan = select_tail_plan(state, root(), old, 'incremental', 398)
    candidate = pages(plan, replies(398))
    first = candidate[0]
    if change == 'count':
        first['page']['count'] += 1
    elif change == 'page':
        first['page']['num'] += 1
    elif change == 'size':
        first['page']['size'] = 10
    elif change == 'empty':
        first['replies'] = []
    elif change == 'order':
        first['replies'][0]['created_at'] = LATER
    elif change == 'duplicate':
        first['replies'][1] = first['replies'][0]
    elif change == 'cross_root':
        first['replies'][0]['root_id'] = '200'
    elif change == 'author':
        first['replies'][0]['author']['uid'] = '3'
    elif change == 'parent':
        first['replies'][0]['parent_id'] = '200'
    elif change == 'root':
        first['root']['author']['uid'] = '3'
    else:
        row = replies(398)[-1]
        old[row['comment_id']] = dict(row, root_id='200')
    with pytest.raises(TailMismatch):
        validate_tail_pages(plan, candidate, old, root(), LATER, state['tail_evidence'])


def test_full_evidence_requires_order_count_identity_and_accepts_equal_times():
    rows = replies(80)
    rows[1]['created_at'] = rows[0]['created_at']
    assert build_tail_evidence(rows, root(), 80, STAMP)
    assert build_tail_evidence(rows, root(), 81, STAMP) is None
    assert build_tail_evidence(list(reversed(rows)), root(), 80, STAMP) is None
    rows[1] = rows[0]
    assert build_tail_evidence(rows, root(), 80, STAMP) is None


def test_chain_updates_overlap_without_claiming_unread_changes():
    old, state = baseline()
    for new, stamp in [(398, LATER), (399, '2026-09-06T10:00:00+00:00')]:
        state['main_count'] = new
        plan = select_tail_plan(state, root(), old, 'incremental', new)
        source = replies(new)
        source[0]['content']['message'] = 'unread change'
        source[1]['comment_id'] = '9999'  # Equal-count replacement outside overlap is invisible.
        candidate = pages(plan, source)
        candidate[0]['replies'][0]['content']['message'] = 'observed update'
        candidate[0]['replies'][0]['display_extra'] = {'ignored': True}
        rows, evidence = validate_tail_pages(plan, candidate, old, root(), stamp,
                                             state['tail_evidence'])
        old.update({row['comment_id']: row for row in rows})
        assert old[source[0]['comment_id']]['content']['message'] == '0'
        assert '9999' not in old
        assert rows[0]['content']['message'] == 'observed update'
        assert evidence['last_full_checked_at'] == STAMP
        assert 'pagination_status' not in evidence
        state.update(tail_evidence=evidence, count=new)


def test_validation_drops_untrusted_extra_fields():
    old, state = baseline()
    evidence = state['tail_evidence']
    clean = deepcopy(evidence)
    evidence['raw_payload'] = {'secret': 'not evidence'}
    evidence['root_identity']['extra'] = 'ignored'
    assert validate_tail_evidence(evidence, root(), old, snapshot_at=STAMP) == clean



def test_selection_accepts_collector_main_core_without_kind():
    old, state = baseline()
    state['main_core'].pop('kind')
    assert select_tail_plan(state, root(), old, 'incremental', 398) is not None


@pytest.mark.parametrize('change', ['root', 'identity', 'time', 'overlap', 'duplicate'])
def test_single_page_rejects_before_staging_without_mutating_seen(change):
    from app.comment_export.tail import validate_tail_page

    old, state = baseline()
    plan = select_tail_plan(state, root(), old, 'incremental', 398)
    payload = pages(plan, replies(398))[0]
    seen = {'555'}
    if change == 'root':
        payload['root']['author']['uid'] = '3'
    elif change == 'identity':
        payload['replies'][0]['author']['uid'] = '3'
    elif change == 'time':
        payload['replies'][0]['created_at'] = LATER
    elif change == 'overlap':
        payload['replies'][0]['comment_id'] = '999'
    else:
        seen.add(payload['replies'][0]['comment_id'])
    before = set(seen)
    with pytest.raises(TailMismatch):
        validate_tail_page(plan, payload, 19, old, root(), LATER,
                           seen_ids=seen, previous_created_at=None)
    assert seen == before


def test_single_page_validates_across_page_order_and_duplicates():
    from app.comment_export.tail import validate_tail_page

    old, state = baseline()
    plan = select_tail_plan(state, root(), old, 'incremental', 398)
    payloads = pages(plan, replies(398))
    first = validate_tail_page(plan, payloads[0], 19, old, root(), LATER,
                               seen_ids=set(), previous_created_at=None)
    seen = {row['comment_id'] for row in first}
    before = set(seen)
    second = validate_tail_page(plan, payloads[1], 20, old, root(), LATER,
                                seen_ids=seen, previous_created_at=first[-1]['created_at'])
    assert second == payloads[1]['replies']
    assert seen == before
    payloads[1]['replies'][0]['created_at'] = replies(1)[0]['created_at']
    with pytest.raises(TailMismatch):
        validate_tail_page(plan, payloads[1], 20, old, root(), LATER,
                           seen_ids=seen, previous_created_at=first[-1]['created_at'])
