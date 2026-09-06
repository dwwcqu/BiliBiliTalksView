import httpx
import pytest
from test_incremental_collection import install_source, raw

from app.comment_export import collector
from app.comment_export.checkpoint import read_checkpoint
from app.comment_export.refresh import refresh


def zero_source(monkeypatch):
    calls = install_source(monkeypatch, ())
    root = dict(raw(100), oid=1, type=1, replies=[])
    monkeypatch.setattr(collector, 'fetch_main', lambda *_: {
        'replies': [root], 'cursor': {'is_end': True, 'all_count': 1}})
    return calls


def test_first_auto_omits_zero_and_preserves_root(monkeypatch, tmp_path):
    calls = zero_source(monkeypatch)
    with httpx.Client() as client:
        rows, meta = refresh('url', tmp_path, client)
    assert calls == []
    assert rows[0]['comment_id'] == '100'
    assert rows[0]['author']['uid'] == '1'
    assert rows[0]['content']['text'] == 'body'
    assert meta['coverage']['status'] == 'partial'
    assert meta['_threads']['100']['zero_completed'] is True
    assert not meta['_threads']['100'].get('last_reply_checked_at')
    assert meta['_refresh']['last_full_scan_completed_at'] is None
    assert meta['_refresh']['zero_reply_schedule']


def test_snapshot_is_frozen_before_resolve_and_survives_resume(monkeypatch, tmp_path):
    calls = zero_source(monkeypatch)
    resolver = collector.resolve_video
    snapshots = []
    def fail(*_):
        snapshots.append(read_checkpoint(tmp_path)[2]['zero_policy_snapshot'])
        raise collector.CollectionStopped('network_error')
    monkeypatch.setattr(collector, 'resolve_video', fail)
    monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T00:00:00Z')
    with httpx.Client() as client:
        with pytest.raises(collector.CollectionStopped):
            refresh('url', tmp_path, client)
        monkeypatch.setattr(collector, 'resolve_video', resolver)
        monkeypatch.setattr(collector, 'now', lambda: '2026-09-07T01:00:00Z')
        _, meta = refresh('url', tmp_path, client, resume=True)
    assert calls == []
    assert meta['_refresh']['zero_policy_snapshot'] == snapshots[0]
    assert meta['coverage']['status'] == 'partial'


@pytest.mark.parametrize('entry', ['collect', 'full'])
def test_strict_entries_still_fetch_details(monkeypatch, tmp_path, entry):
    calls = zero_source(monkeypatch)
    with httpx.Client() as client:
        if entry == 'collect':
            collector.collect('url', tmp_path, client)
        else:
            refresh('url', tmp_path, client, mode='full')
    assert calls == [('100', 1)]


def test_first_then_incremental_then_due_verification(monkeypatch, tmp_path):
    calls = zero_source(monkeypatch)
    monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T00:00:00Z')
    with httpx.Client() as client:
        _, first = refresh('url', tmp_path / 'first', client)
        monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T20:00:00Z')
        _, second = refresh('url', tmp_path / 'second', client,
                            baseline_work=tmp_path / 'first')
        assert second['_refresh']['mode'] == 'incremental'
        assert calls == []
        assert second['_refresh']['zero_policy_snapshot']['verify_due_at'] == (
            first['_refresh']['zero_policy_snapshot']['verify_due_at'])
        monkeypatch.setattr(collector, 'now', lambda: '2026-09-07T01:00:00Z')
        _, third = refresh('url', tmp_path / 'third', client,
                           baseline_work=tmp_path / 'second')
    assert calls == [('100', 1)]
    assert third['_refresh']['zero_policy_snapshot']['zero_policy'] == 'verify'
    assert not third['_threads']['100'].get('zero_completed')


def test_main_interruption_preserves_conflicting_duplicate(monkeypatch, tmp_path):
    calls = zero_source(monkeypatch)
    failed = False
    root = dict(raw(100), oid=1, type=1, replies=[])
    def main(source, cursor, *_):
        nonlocal failed
        if cursor == '' and not failed:
            return {'replies': [dict(root, rcount=1)], 'cursor': {
                'is_end': False, 'pagination_reply': {'next_offset': 'next'}}}
        if not failed:
            failed = True
            raise collector.CollectionStopped('network_error')
        return {'replies': [root], 'cursor': {'is_end': True, 'all_count': 1}}
    monkeypatch.setattr(collector, 'fetch_main', main)
    with httpx.Client() as client:
        refresh('url', tmp_path, client)
        assert calls == []
        _, result = refresh('url', tmp_path, client, resume=True)
    assert calls == [('100', 1)]
    state = result['_threads']['100']
    assert state['zero_main_observation']['conflict'] is True
    assert state['zero_reply_history']['ever_nonzero_observed'] is True


def test_zero_committed_before_later_floor_failure_is_not_repeated(monkeypatch, tmp_path):
    calls = zero_source(monkeypatch)
    roots = [dict(raw(100), oid=1, type=1, replies=[]), dict(raw(200), oid=1, type=1)]
    roots[1]['rcount'] = None
    monkeypatch.setattr(collector, 'fetch_main', lambda *_: {
        'replies': roots, 'cursor': {'is_end': True, 'all_count': 2}})
    fail = True
    def replies(source, rid, page, client):
        calls.append((rid, page))
        if fail:
            raise collector.CollectionStopped('network_error')
        return {'root': roots[1], 'page': {'num': 1, 'size': 20, 'count': 0}, 'replies': []}
    monkeypatch.setattr(collector, 'fetch_replies', replies)
    with httpx.Client() as client:
        _, partial = refresh('url', tmp_path, client)
        assert partial['_threads']['100']['zero_completed'] is True
        assert 'zero_reply_schedule' not in partial['_refresh']
        fail = False
        _, finished = refresh('url', tmp_path, client, resume=True)
    assert calls == [('200', 1), ('200', 1)]
    assert finished['_refresh']['zero_reply_schedule']


def test_unavailable_annotation_preserves_monotonic_history():
    from app.comment_export.availability import annotate_unavailable
    from app.comment_export.diagnostics import FailureDetail
    state = {'pagination_status': 'partial', 'zero_reply_history': {
        'version': 1, 'known': True, 'ever_nonzero_observed': True,
        'ever_source_unavailable': False}}
    meta = {'video_id': 'bilibili:video:1', '_threads': {'100': state}, 'coverage': {}}
    detail = FailureDetail(endpoint='/x/v2/reply/reply', phase='replies',
        http_status=200, api_code=12022, category='resource_unavailable',
        video_id='bilibili:video:1', target={'root_id': '100', 'page': 1})
    annotate_unavailable(meta, '100', 1, detail)
    assert state['zero_reply_history']['ever_source_unavailable'] is True
    assert state['zero_reply_history']['ever_nonzero_observed'] is True


def test_n_zero_floors_save_exactly_n_detail_requests(monkeypatch, tmp_path):
    zero_source(monkeypatch)
    roots = [dict(raw(cid), oid=1, type=1, replies=[]) for cid in range(100, 107)]
    monkeypatch.setattr(collector, 'fetch_main', lambda *_: {
        'replies': roots, 'cursor': {'is_end': True, 'all_count': 7}})
    calls = []
    def replies(source, rid, page, client):
        calls.append((rid, page))
        return {'root': roots[int(rid)-100],
                'page': {'num': page, 'size': 20, 'count': 0}, 'replies': []}
    monkeypatch.setattr(collector, 'fetch_replies', replies)
    with httpx.Client() as client:
        observed, _ = refresh('url', tmp_path / 'observe', client)
        assert calls == []
        verified, _ = refresh('url', tmp_path / 'verify', client, mode='full')
    assert calls == [(str(cid), 1) for cid in range(100, 107)]
    assert [(r['comment_id'], r['author'], r['content']) for r in observed] == [
        (r['comment_id'], r['author'], r['content']) for r in verified]


def test_legacy_resume_does_not_gain_first_auto_exception(monkeypatch, tmp_path):
    from app.comment_export.checkpoint import Checkpoint
    calls = zero_source(monkeypatch)
    cp = Checkpoint(tmp_path / 'work.sqlite3')
    try:
        cp.set_progress({'refresh_request': {'version': 1, 'requested_mode': 'auto'},
                         'input_url': 'url', 'max_requests': 100, 'requests': 0})
    finally:
        cp.close()
    with httpx.Client() as client:
        _, result = refresh('url', tmp_path, client, resume=True)
    assert calls == [('100', 1)]
    assert 'zero_policy_snapshot' not in result['_refresh']
    assert 'zero_reply_schedule' not in result['_refresh']


@pytest.mark.parametrize('patch', [{'rcount': False}, {'count': 1}, {'replies': [{}]},
                                  {'oid': 2}, {'type': 2}])
def test_invalid_raw_zero_falls_back_to_detail(monkeypatch, tmp_path, patch):
    calls = zero_source(monkeypatch)
    root = dict(raw(100), oid=1, type=1, replies=[])
    root.update(patch)
    monkeypatch.setattr(collector, 'fetch_main', lambda *_: {
        'replies': [root], 'cursor': {'is_end': True, 'all_count': 1}})
    with httpx.Client() as client:
        _, result = refresh('url', tmp_path, client)
    assert calls == [('100', 1)]
    assert not result['_threads']['100'].get('zero_completed')


def test_full_timestamp_requires_actual_detail_completion_evidence(monkeypatch, tmp_path):
    from app.comment_export.incremental import finish_tracking
    zero_source(monkeypatch)
    with httpx.Client() as client:
        rows, meta = refresh('url', tmp_path, client, mode='full')
    info = meta['_refresh']
    prior = info['last_full_scan_completed_at']
    meta['captured_to'] = '2099-01-01T00:00:00Z'
    meta['_threads']['100'].pop('last_complete_at')
    finish_tracking(meta, rows, {'finished': True, 'main_done': True})
    assert info['last_full_scan_completed_at'] == prior


def test_mixed_zero_tail_full_and_existing_reuse(monkeypatch, tmp_path):
    from app.comment_export.checkpoint import read_checkpoint

    phase = {'updated': False}
    calls = []
    monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T00:00:00Z')
    monkeypatch.setattr(collector, 'resolve_video', lambda *_: {
        'source': {'platform': 'bilibili', 'aid': '1', 'oid': '1', 'bvid': 'BVfake',
                   'episode_id': None, 'comment_type': 1},
        'canonical_url': 'https://www.bilibili.com/video/BVfake', 'title': 'mixed'})
    monkeypatch.setattr(collector, 'get_signing_keys', lambda *_: ('a', 'b'))
    def root(rid, count):
        return dict(raw(rid, count=count), oid=1, type=1, replies=[])
    def main(*_):
        entries = [root(100, 62 if phase['updated'] else 61), root(200, 0)]
        if phase['updated']:
            entries += [root(300, 0), root(400, 1)]
        return {'replies': entries, 'cursor': {'is_end': True, 'all_count': 70}}
    def details(source, rid, page, client):
        calls.append((rid, page))
        ids = list(range(101, 163 if phase['updated'] else 162)) if rid == '100' else (
            [401] if rid == '400' else [])
        return {'root': root(int(rid), len(ids)),
                'page': {'num': page, 'size': 20, 'count': len(ids)},
                'replies': [raw(cid, int(rid)) for cid in ids[(page-1)*20:page*20]]}
    monkeypatch.setattr(collector, 'fetch_main', main)
    monkeypatch.setattr(collector, 'fetch_replies', details)
    with httpx.Client() as client:
        collector.collect('url', tmp_path / 'base', client)
        old_rows, _, _ = read_checkpoint(tmp_path / 'base')
        old_first = next(r for r in old_rows if r['comment_id'] == '101')
        phase['updated'] = True
        calls.clear()
        monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T01:00:00Z')
        rows, meta = refresh('url', tmp_path / 'next', client, baseline_work=tmp_path / 'base')
    assert calls == [('100', 3), ('100', 4), ('400', 1)]
    assert meta['_threads']['100']['tail_completed']
    assert meta['_threads']['200']['skip_refresh']
    assert meta['_threads']['300']['zero_completed']
    assert meta['_threads']['400']['reply_verification'] == 'checked_now'
    assert meta['_refresh']['zero_reply_schedule']
    assert next(r for r in rows if r['comment_id'] == '101') == dict(
        old_first, export_id=meta['export_id'])
    assert {r['comment_id'] for r in rows} == {
        '100', '200', '300', '400', '401', *map(str, range(101, 163))}
    assert meta['coverage']['status'] == 'partial'


@pytest.mark.parametrize('corruption', ['version', 'full_observe', 'work_mismatch', 'job_mismatch'])
def test_invalid_restored_snapshot_disables_zero_optimization(monkeypatch, tmp_path, corruption):
    from copy import deepcopy
    from uuid import uuid4

    from app.comment_export.checkpoint import Checkpoint
    from app.comment_export.zero_schedule import decide_policy
    calls = zero_source(monkeypatch)
    snapshot = decide_policy('auto', collector.now(), 24, None,
                              work_id=str(uuid4()), baseline_binding=None)
    progress = {'refresh_request': {'version': 1, 'requested_mode': 'auto'},
                'input_url': 'url', 'max_requests': 100, 'requests': 0,
                'zero_policy_snapshot': snapshot}
    if corruption == 'version':
        snapshot['version'] = 99
    elif corruption == 'full_observe':
        snapshot['requested_mode'] = 'full'
    elif corruption == 'job_mismatch':
        progress['job_id'] = str(uuid4())
    else:
        # Initialize a real unfinished checkpoint with a conflicting metadata snapshot.
        with httpx.Client() as client:
            _, metadata = refresh('url', tmp_path / 'seed', client)
        calls.clear()
        metadata = deepcopy(metadata)
        metadata['_threads'] = {}
        metadata['_refresh']['observed_ids'] = []
        metadata['_refresh']['observed_main_ids'] = []
        metadata['_refresh'].pop('zero_reply_schedule', None)
        metadata['_refresh']['zero_policy_snapshot']['work_id'] = str(uuid4())
        progress['metadata'] = metadata
    cp = Checkpoint(tmp_path / 'work.sqlite3')
    try:
        cp.set_progress(progress)
    finally:
        cp.close()
    with httpx.Client() as client:
        _, result = refresh('url', tmp_path, client, resume=True)
    assert calls == [('100', 1)]
    assert 'zero_reply_schedule' not in result['_refresh']


def test_prepare_refresh_scans_input_a_bounded_number_of_times(frozen_case):
    from copy import deepcopy

    from app.comment_export.incremental import prepare_refresh
    rows, metadata = deepcopy(frozen_case)
    template = next(row for row in rows if row['kind'] == 'root')
    class CountingRows(list):
        visits = 0
        def __iter__(self):
            for row in super().__iter__():
                self.visits += 1
                yield row
    records = CountingRows(dict(template, comment_id=str(cid), root_id=str(cid))
                           for cid in range(100, 200))
    metadata['_threads'] = {row['root_id']: {'pagination_status': 'partial', 'count': 0}
                            for row in records}
    records.visits = 0
    prepare_refresh(records, metadata, {}, 'full', metadata['captured_to'], 24)
    assert records.visits <= 10 * len(records)
