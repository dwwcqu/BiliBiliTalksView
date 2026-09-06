"""Optional tail evidence survives storage without changing the public export contract."""
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.comment_export.checkpoint import read_checkpoint
from app.comment_export.export import build_batch
from app.comment_export.incremental import checked_thread, start_tracking, track_rows
from app.storage.baseline import freeze_baseline
from app.storage.codec import decode_json, encode_json
from app.storage.frozen import freeze_batch
from app.storage.handoff import materialize, prepare_handoff
from app.storage.schema import discussion_states, videos


def tail_case(frozen_case):
    from app.comment_export.tail import build_tail_evidence

    _, metadata = deepcopy(frozen_case)
    root = next(deepcopy(r) for r in frozen_case[0] if r['comment_id'] == '100')
    replies = []
    for index in range(81):
        row = deepcopy(root)
        row.update(comment_id=str(1001 + index), parent_id='100', kind='reply')
        row['author'] = {'uid': '2', 'nickname': 'sample'}
        row['reply_relation'] = {'status': 'source', 'target_uid': None}
        replies.append(row)
    rows = [root] + replies
    metadata['_threads'] = {'100': {'pagination_status': 'verified', 'count': 81}}
    metadata['source_reported_count'] = len(rows)
    start_tracking(metadata)
    track_rows(metadata, [root], 'main')
    track_rows(metadata, replies, 'replies')
    metadata['_refresh'].update(
        last_full_scan_completed_at=metadata['captured_to'], full_scan_incomplete=False)
    state = metadata['_threads']['100']
    checked_thread(state, root, metadata['captured_to'])
    state['tail_evidence'] = build_tail_evidence(replies, root, 81, metadata['captured_to'])
    assert state['tail_evidence'] is not None
    return rows, metadata


def save(conn, rows, metadata, tmp_path, baseline_version=0):
    with freeze_batch(build_batch(rows, metadata, tmp_path / 'batch'),
                      tmp_path / 'frozen') as frozen:
        work_id = metadata.get('_refresh', {}).get('zero_policy_snapshot', {}).get(
            'work_id', str(uuid4()))
        handoff = prepare_handoff(frozen, rows, metadata, work_id, baseline_version)
        result = materialize(conn, frozen, handoff, lambda *_: None)
    return result, handoff


def test_tail_evidence_roundtrips_but_stays_out_of_export(conn, frozen_case, tmp_path):
    rows, metadata = tail_case(frozen_case)
    _, handoff = save(conn, rows, metadata, tmp_path)
    expected = metadata['_threads']['100']['tail_evidence']
    stored = json.loads(handoff.context_json)['evidence']['threads']['100']
    assert stored['tail_evidence'] == expected
    freeze_baseline(conn, metadata['video_id'], tmp_path / 'baseline')
    restored = read_checkpoint(tmp_path / 'baseline')[1]
    assert restored['_threads']['100']['tail_evidence'] == expected
    for file in (tmp_path / 'batch').rglob('*'):
        if file.is_file():
            assert 'tail_evidence' not in file.read_text(encoding='utf-8')
    assert 'tail_pages' not in handoff.context_json


@pytest.mark.parametrize('corruption', ['version', 'anchor', 'future', 'extra', 'historical_count', 'historical_root'])
def test_invalid_optional_evidence_is_dropped_on_baseline(
    conn, frozen_case, tmp_path, corruption
):
    rows, metadata = tail_case(frozen_case)
    result, _ = save(conn, rows, metadata, tmp_path)
    with conn.begin():
        value = conn.scalar(select(discussion_states.c.refresh_context).where(
            discussion_states.c.state_id == result['state_id']))
        context = decode_json(value)
        tail = deepcopy(metadata['_threads']['100']['tail_evidence'])
        if corruption == 'version':
            tail['version'] = 900
        elif corruption == 'anchor':
            tail['anchor_ids'] = ['999999']
        elif corruption == 'future':
            tail['latest_tail_checked_at'] = '2099-01-01T00:00:00Z'
        elif corruption == 'historical_count':
            context['evidence']['threads']['100']['checked_count'] = 999
        elif corruption == 'historical_root':
            context['evidence']['threads']['100']['checked_root'] = '0' * 64
        else:
            tail['unexpected_payload'] = {'body': 'must not survive optional evidence'}
        context['evidence']['threads']['100']['tail_evidence'] = tail
        conn.execute(update(discussion_states).where(
            discussion_states.c.state_id == result['state_id']).values(
                refresh_context=encode_json(context)))
    freeze_baseline(conn, metadata['video_id'], tmp_path / 'baseline')
    restored_rows, restored, _ = read_checkpoint(tmp_path / 'baseline')
    assert len(restored_rows) == len(rows)
    tail = restored['_threads']['100'].get('tail_evidence')
    if corruption == 'extra':
        assert tail is None or 'unexpected_payload' not in tail
    else:
        assert tail is None


def test_tail_handoff_conflict_preserves_visible_state(conn, frozen_case, tmp_path):
    from app.storage.errors import StorageError

    rows, metadata = tail_case(frozen_case)
    first, _ = save(conn, rows, metadata, tmp_path / 'first')
    later = deepcopy(metadata)
    later['export_id'] = str(uuid4())
    later_rows = [dict(r, export_id=later['export_id']) for r in rows]
    with freeze_batch(build_batch(later_rows, later, tmp_path / 'later'),
                      tmp_path / 'frozen-later') as frozen:
        handoff = prepare_handoff(frozen, later_rows, later, str(uuid4()), 0)
        with pytest.raises(StorageError, match='baseline_changed'):
            materialize(conn, frozen, handoff, lambda *_: None)
    with conn.begin():
        assert conn.scalar(select(videos.c.current_state_id)) == first['state_id']


def test_missing_tail_evidence_remains_readable(conn, frozen_case, tmp_path):
    rows, metadata = tail_case(frozen_case)
    metadata['_threads']['100'].pop('tail_evidence')
    save(conn, rows, metadata, tmp_path)
    freeze_baseline(conn, metadata['video_id'], tmp_path / 'baseline')
    assert 'tail_evidence' not in read_checkpoint(tmp_path / 'baseline')[1]['_threads']['100']


def test_partial_tail_preserves_full_check_time_across_storage(conn, frozen_case, tmp_path):
    from app.comment_export.incremental import prepare_refresh, select_threads
    from app.comment_export.tail import build_tail_evidence

    rows, metadata = tail_case(frozen_case)
    root = rows[0]
    original_full = metadata['captured_to']
    latest = '2026-09-05T10:00:00Z'
    new = deepcopy(rows[-1])
    new.update(comment_id='1082', collected_at=latest)
    rows.append(new)
    metadata.update(captured_to=latest, exported_at=latest)
    state = metadata['_threads']['100']
    tail = build_tail_evidence(rows[1:], root, 82, latest)
    assert tail is not None
    tail['last_full_checked_at'] = original_full
    state.update(count=82, pagination_status='partial', reply_verification='reused_unverified',
                 tail_evidence=tail)
    metadata['_refresh'].update(mode='incremental', requested_mode='auto',
                               observed_ids=['100'] + [r['comment_id'] for r in rows[-22:]])
    metadata['coverage'].update(status='partial', replies_pagination='partial',
                                reasons=['replies_incomplete'])
    _, handoff = save(conn, rows, metadata, tmp_path)
    evidence = json.loads(handoff.context_json)['evidence']['threads']['100']
    assert evidence['checked_count'] == 81
    assert evidence['tail_evidence']['source_count'] == 82
    assert evidence['last_complete_at'] == original_full
    freeze_baseline(conn, metadata['video_id'], tmp_path / 'baseline')
    saved, restored, progress = read_checkpoint(tmp_path / 'baseline')
    assert restored['_threads']['100']['tail_evidence'] == tail
    _, follow, _ = prepare_refresh(saved, restored, progress, 'auto',
                                    '2026-09-05T11:00:00Z', 24)
    follow['_refresh']['observed_main_ids'] = ['100']
    follow['_threads']['100'].update(main_count=82, main_core=root)
    select_threads(follow, {r['comment_id']: r for r in saved})
    assert follow['_threads']['100']['skip_refresh'] is True
    assert follow['_threads']['100']['last_complete_at'] == original_full


def test_two_tail_appends_materialize_and_restore_without_resetting_full_time(
    conn, frozen_case, tmp_path
):
    from app.comment_export.incremental import prepare_refresh
    from app.comment_export.tail import select_tail_plan, validate_tail_pages

    rows, metadata = tail_case(frozen_case)
    first_full = metadata['captured_to']
    save(conn, rows, metadata, tmp_path / 'full')
    for count, stamp in [(82, '2026-09-05T10:00:00Z'), (83, '2026-09-05T11:00:00Z')]:
        target = tmp_path / f'baseline-{count}'
        frozen_baseline = freeze_baseline(conn, metadata['video_id'], target)
        prior_rows, prior, prior_progress = read_checkpoint(target)
        seeds, metadata, _ = prepare_refresh(prior_rows, prior, prior_progress, 'auto', stamp, 24)
        old = {r['comment_id']: r for r in seeds}
        root = dict(old['100'], collected_at=stamp)
        replies = sorted((r for r in seeds if r['kind'] == 'reply'),
                         key=lambda r: int(r['comment_id']))
        added = deepcopy(replies[-1])
        added.update(comment_id=str(1000 + count), collected_at=stamp)
        source_replies = [dict(r, collected_at=stamp) for r in replies] + [added]
        state = metadata['_threads']['100']
        state.update(main_observed=True, main_core=root, main_count=count)
        plan = select_tail_plan(state, root, old, 'incremental', count)
        assert plan is not None
        payloads = [
            {'page': {'num': n, 'size': 20, 'count': count}, 'root': root,
             'replies': source_replies[(n - 1) * 20:n * 20]}
            for n in range(plan.start_page, plan.last_page + 1)
        ]
        changed, evidence = validate_tail_pages(
            plan, payloads, old, root, stamp, state['tail_evidence'])
        merged = dict(old)
        merged['100'] = root
        merged.update({r['comment_id']: r for r in changed})
        rows = list(merged.values())
        track_rows(metadata, [root], 'main')
        track_rows(metadata, changed, 'replies')
        state.update(count=count, tail_evidence=evidence, pagination_status='partial',
                     reply_verification='reused_unverified')
        metadata['coverage'].update(status='partial', main_pagination='verified',
                                    replies_pagination='partial', reasons=['replies_incomplete'])
        metadata['source_reported_count'] = len(rows)
        _, handoff = save(conn, rows, metadata, tmp_path / f'append-{count}',
                          frozen_baseline['cache_version'])
        persisted = json.loads(handoff.context_json)['evidence']['threads']['100']
        assert persisted['tail_evidence']['source_count'] == count
        assert persisted['tail_evidence']['last_full_checked_at'] == first_full
        assert persisted['last_complete_at'] == first_full
        assert persisted['checked_count'] == 81
    freeze_baseline(conn, metadata['video_id'], tmp_path / 'final')
    saved, restored, _ = read_checkpoint(tmp_path / 'final')
    assert len(saved) == 84
    assert restored['_threads']['100']['tail_evidence']['source_count'] == 83
