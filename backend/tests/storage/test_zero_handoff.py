"""Zero observations are optional, bound to a state, and survive only validated handoff."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from test_zero_collection import zero_source

from app.comment_export import collector
from app.comment_export.checkpoint import read_checkpoint
from app.comment_export.export import build_batch
from app.comment_export.refresh import refresh
from app.storage.baseline import freeze_baseline
from app.storage.codec import decode_json, encode_json
from app.storage.frozen import freeze_batch
from app.storage.handoff import materialize, prepare_handoff
from app.storage.schema import discussion_states, videos


def observed_case(monkeypatch, tmp_path):
    calls = zero_source(monkeypatch)
    monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T00:00:00Z')
    with httpx.Client() as client:
        rows, metadata = refresh('https://www.bilibili.com/video/BVfake', tmp_path / 'work', client)
    assert calls == []
    assert metadata['_refresh']['zero_reply_schedule']
    return rows, metadata


def save(conn, rows, metadata, tmp_path, version=0):
    # Publication may have a different export ID from the stable collection work ID.
    exported = dict(metadata, export_id=str(uuid4()))
    # Keep fixture hour labels on the same controlled clock as captured_to.
    exported['hour_bucket'] = datetime.fromisoformat(metadata['captured_to']).astimezone(
        timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:00:00+08:00')
    records = [dict(r, export_id=exported['export_id']) for r in rows]
    work_id = metadata['_refresh']['zero_policy_snapshot']['work_id']
    with freeze_batch(build_batch(records, exported, tmp_path / 'batch'),
                      tmp_path / 'frozen') as frozen:
        handoff = prepare_handoff(frozen, records, exported, work_id, version)
        result = materialize(conn, frozen, handoff, lambda *_: None)
    return result, handoff


def test_zero_roundtrip_preserves_deadline_and_omits_public_internal_fields(
    conn, monkeypatch, tmp_path
):
    from app.api.discussions import summary
    from app.comment_export.zero_schedule import validate_schedule

    rows, metadata = observed_case(monkeypatch, tmp_path)
    result, handoff = save(conn, rows, metadata, tmp_path)
    context = json.loads(handoff.context_json)
    assert context['zero_reply_summary']['observed_threads'] == 1
    assert context['evidence']['threads']['100']['zero_reply_history']['known'] is True
    freeze_baseline(conn, metadata['video_id'], tmp_path / 'baseline')
    saved, restored, _ = read_checkpoint(tmp_path / 'baseline')
    proof = restored['_refresh']['zero_reply_schedule']
    assert validate_schedule(proof, restored, saved) == proof
    assert proof['verify_due_at'] == metadata['_refresh']['zero_reply_schedule']['verify_due_at']
    with conn.begin():
        row = conn.execute(select(discussion_states).where(
            discussion_states.c.state_id == result['state_id'])).mappings().one()
    assert summary(row)['zero_reply_observed_threads'] == 1
    for file in (tmp_path / 'batch').rglob('*'):
        if file.is_file():
            contents = file.read_text(encoding='utf-8')
            assert 'zero_reply_evidence' not in contents
            assert 'zero_reply_schedule' not in contents
            assert 'zero_reply_history' not in contents


def test_database_roundtrip_allows_next_auto_but_does_not_postpone_due(
    conn, monkeypatch, tmp_path
):
    rows, metadata = observed_case(monkeypatch, tmp_path)
    save(conn, rows, metadata, tmp_path / 'first')
    for hour in [20, 23]:
        baseline = tmp_path / f'baseline-{hour}'
        pointer = freeze_baseline(conn, metadata['video_id'], baseline)
        calls = zero_source(monkeypatch)
        monkeypatch.setattr(collector, 'now', lambda h=hour: f'2026-09-06T{h}:00:00Z')
        with httpx.Client() as client:
            rows, metadata = refresh('https://www.bilibili.com/video/BVfake', tmp_path / f'work-{hour}', client,
                                      baseline_work=baseline)
        assert calls == []
        assert metadata['_refresh']['mode'] == 'incremental'
        save(conn, rows, metadata, tmp_path / f'save-{hour}', pointer['cache_version'])
    baseline = tmp_path / 'due-baseline'
    freeze_baseline(conn, metadata['video_id'], baseline)
    calls = zero_source(monkeypatch)
    monkeypatch.setattr(collector, 'now', lambda: '2026-09-07T01:00:00Z')
    with httpx.Client() as client:
        _, metadata = refresh('https://www.bilibili.com/video/BVfake', tmp_path / 'due', client, baseline_work=baseline)
    assert calls == [('100', 1)]
    assert metadata['_refresh']['zero_policy_snapshot']['zero_policy'] == 'verify'


@pytest.mark.parametrize('kind', ['schedule', 'history', 'evidence', 'snapshot'])
def test_bad_optional_extension_does_not_break_core_baseline(
    conn, monkeypatch, tmp_path, kind
):
    rows, metadata = observed_case(monkeypatch, tmp_path)
    result, _ = save(conn, rows, metadata, tmp_path)
    with conn.begin():
        context = decode_json(conn.scalar(select(discussion_states.c.refresh_context).where(
            discussion_states.c.state_id == result['state_id'])))
        if kind == 'schedule':
            context['evidence']['refresh']['zero_reply_schedule']['version'] = 99
        elif kind == 'snapshot':
            context['evidence']['refresh']['zero_policy_snapshot']['version'] = 99
        elif kind == 'evidence':
            context['evidence']['threads']['100']['zero_reply_evidence']['root_id'] = '999'
        else:
            context['evidence']['threads']['100']['zero_reply_history'].update(
                version=99, ever_nonzero_observed=True)
        conn.execute(update(discussion_states).where(
            discussion_states.c.state_id == result['state_id']).values(
                refresh_context=encode_json(context)))
    freeze_baseline(conn, metadata['video_id'], tmp_path / 'restored')
    saved, restored, _ = read_checkpoint(tmp_path / 'restored')
    assert [r['comment_id'] for r in saved] == ['100']
    assert not restored['_refresh'].get('zero_reply_schedule')
    if kind == 'history':
        history = restored['_threads']['100']['zero_reply_history']
        assert history['known'] is False and history['ever_nonzero_observed'] is True


def test_forged_current_zero_evidence_cannot_be_published(conn, monkeypatch, tmp_path):
    from app.storage.errors import StorageError

    rows, metadata = observed_case(monkeypatch, tmp_path)
    metadata['_threads']['100']['zero_reply_history']['ever_nonzero_observed'] = True
    with pytest.raises(StorageError, match='invalid_collection_handoff'):
        save(conn, rows, metadata, tmp_path)
    with conn.begin():
        assert conn.scalar(select(videos.c.video_id)) is None


def test_atomic_guard_rejection_keeps_previous_zero_state(conn, monkeypatch, tmp_path):
    from app.storage.errors import StorageError

    rows, metadata = observed_case(monkeypatch, tmp_path)
    old, _ = save(conn, rows, metadata, tmp_path / 'old')
    with conn.begin():
        version = conn.scalar(select(videos.c.cache_version))
    newer = deepcopy(metadata)
    newer['export_id'] = str(uuid4())
    new_rows = [dict(r, export_id=newer['export_id']) for r in rows]
    with freeze_batch(build_batch(new_rows, newer, tmp_path / 'new'),
                      tmp_path / 'frozen-new') as frozen:
        handoff = prepare_handoff(frozen, new_rows, newer,
                                  newer['_refresh']['zero_policy_snapshot']['work_id'], version)
        with pytest.raises(StorageError, match='commit_guard_rejected'):
            materialize(conn, frozen, handoff, lambda *_: False)
    with conn.begin():
        assert conn.scalar(select(videos.c.working_state_id)) == old['state_id']


@pytest.mark.parametrize('flag', ['ever_nonzero_observed', 'ever_source_unavailable'])
def test_materialize_does_not_erase_existing_counterevidence(
    conn, monkeypatch, tmp_path, flag
):
    from app.storage.errors import StorageError

    rows, metadata = observed_case(monkeypatch, tmp_path)
    old, _ = save(conn, rows, metadata, tmp_path / 'old')
    with conn.begin():
        context = decode_json(conn.scalar(select(discussion_states.c.refresh_context)))
        context['evidence']['threads']['100']['zero_reply_history'][flag] = True
        conn.execute(update(discussion_states).values(refresh_context=encode_json(context)))
    with conn.begin():
        version = conn.scalar(select(videos.c.cache_version))
    with pytest.raises(StorageError, match='zero_history_regression'):
        save(conn, rows, metadata, tmp_path / 'regression', version)
    with conn.begin():
        assert conn.scalar(select(videos.c.working_state_id)) == old['state_id']


def test_http_current_and_partial_expose_their_own_zero_counts(
    conn, db_engine, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient
    from test_incremental_collection import raw

    from app.main import create_app

    zero_source(monkeypatch)
    monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T00:00:00Z')
    with httpx.Client() as client:
        rows, metadata = refresh('https://www.bilibili.com/video/BVfake',
                                  tmp_path / 'strict', client, mode='full')
    old, _ = save(conn, rows, metadata, tmp_path / 'save-strict')
    assert old['published'] is True
    pointer = freeze_baseline(conn, metadata['video_id'], tmp_path / 'base')
    monkeypatch.setattr(collector, 'now', lambda: '2026-09-06T01:00:00Z')
    roots = [dict(raw(rid), oid=1, type=1, replies=[]) for rid in (100, 200)]
    monkeypatch.setattr(collector, 'fetch_main', lambda *_: {
        'replies': roots, 'cursor': {'is_end': True, 'all_count': 2}})
    with httpx.Client() as client:
        rows, metadata = refresh('https://www.bilibili.com/video/BVfake',
            tmp_path / 'new', client, baseline_work=tmp_path / 'base')
    newer, _ = save(conn, rows, metadata, tmp_path / 'save-new', pointer['cache_version'])
    assert newer['published'] is False
    with TestClient(create_app(database_engine=db_engine)) as client:
        response = client.get('/api/v1/videos/bilibili:video:1')
    assert response.status_code == 200
    body = response.json()
    assert body['state']['state_id'] == old['state_id']
    assert body['state']['zero_reply_observed_threads'] == 0
    assert body['partial_state']['state_id'] == newer['state_id']
    assert body['partial_state']['zero_reply_observed_threads'] == 1
