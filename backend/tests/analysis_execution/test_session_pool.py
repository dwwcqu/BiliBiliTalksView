import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.analysis_execution.command import CliRequest
from app.analysis_packets.codec import json_bytes, loads


def make_pool(tmp_path):
    from app.analysis_execution.session_pool import create_session_pool
    return create_session_pool(tmp_path, video_id='BV123', model='model', provider='test',
                               members=[{'member_id': 'worker', 'role': 'user_initial',
                                         'role_sha256': 'a' * 64}],
                               prior_estimated_cost_usd=Decimal('.1'))


def req(tmp_path):
    return CliRequest(executable=Path(sys.executable), cwd=tmp_path,
                      session_id=make_pool(tmp_path)['members'][0]['agent_id'],
                      attempt_id=str(uuid4()), model='model',
                      prompt='{"task":"hello"}', budget_usd=Decimal('.1'),
                      timeout_seconds=5, max_output_bytes=65536, resume=False)


def fake_boundary(monkeypatch, *, cost='.05', fail=False):
    from app.analysis_execution import runner
    def run(argv, **kwargs):
        sid = argv[argv.index('--resume') + 1] if '--resume' in argv else argv[argv.index('--session-id') + 1]
        events = [{'type': 'system', 'subtype': 'init', 'session_id': sid,
                   'model': 'model', 'cwd': str(kwargs['cwd']), 'claude_code_version': '2.1.261'},
                  {'type': 'result', 'subtype': 'error_during_execution' if fail else 'success',
                   'is_error': fail, 'session_id': sid, 'total_cost_usd': Decimal(cost),
                   'result': '{"ok":true}'}]
        return {'stdout': b''.join(json_bytes(e) for e in events), 'stderr': b'',
                'returncode': 1 if fail else 0, 'stop_reason': None}
    monkeypatch.setattr(runner, 'run_process', run)


def turn(tmp_path, request, **kwargs):
    from app.analysis_execution.session_pool import run_pool_turn
    return run_pool_turn(tmp_path, 'worker', request=request, environment={},
                         expected_model='model', spend_limit=Decimal(1), authorized=True, **kwargs)


def test_pool_registration_is_offline_and_immutable(tmp_path):
    from app.analysis_execution.session_pool import create_session_pool, load_session_pool
    pool = make_pool(tmp_path)
    assert make_pool(tmp_path) == pool == load_session_pool(tmp_path)
    assert str(UUID(pool['main']['agent_id'])) == pool['main']['agent_id']
    assert pool['main']['agent_id'] != pool['members'][0]['agent_id']
    assert not (tmp_path / 'sessions/pool-calls').exists()
    with pytest.raises(ValueError, match='pool_configuration_conflict'):
        create_session_pool(tmp_path, video_id='other', model='model', provider='test',
                            members=[{'member_id':'worker','role':'user_initial','role_sha256':'a'*64}])


def test_create_then_resume_same_id_and_reuse_attempt(tmp_path, monkeypatch):
    pool = make_pool(tmp_path)
    fake_boundary(monkeypatch)
    request = req(tmp_path)
    first = turn(tmp_path, request)
    assert first['status'] == 'succeeded'
    assert first['request'].session_id == pool['members'][0]['agent_id']
    assert first['request'].resume is False
    assert first['result'] == {'ok': True}
    assert turn(tmp_path, request)['reused'] is True
    second = turn(tmp_path, replace(request, attempt_id=str(uuid4())))
    assert second['request'].session_id == first['request'].session_id
    assert second['request'].resume is True
    with pytest.raises(ValueError, match='pool_request_changed'):
        turn(tmp_path, replace(request, prompt='changed'))


def test_unknown_or_incomplete_call_blocks_new_paid_turn(tmp_path):
    pool = make_pool(tmp_path)
    pending = tmp_path / 'sessions/pool-calls' / pool['members'][0]['agent_id'] / str(uuid4())
    pending.mkdir(parents=True)
    (pending / 'request.json').write_text('{}')
    got = turn(tmp_path, req(tmp_path))
    assert got['status'] == 'blocked'


def test_failed_attempt_never_replaced_and_cost_counts(tmp_path, monkeypatch):
    pool = make_pool(tmp_path)
    fake_boundary(monkeypatch, fail=True)
    request = req(tmp_path)
    got = turn(tmp_path, request)
    assert got['status'] == 'blocked'
    assert turn(tmp_path, request)['reused'] is True
    assert make_pool(tmp_path)['members'] == pool['members']


def test_prior_and_overrun_headroom_block_additional_spend(tmp_path, monkeypatch):
    from app.analysis_execution.session_pool import run_pool_turn
    make_pool(tmp_path)
    fake_boundary(monkeypatch, cost='.4')
    request = req(tmp_path)
    assert turn(tmp_path, request)['status'] == 'succeeded'
    got = run_pool_turn(tmp_path, 'main', request=replace(request, attempt_id=str(uuid4()),
                                        session_id=make_pool(tmp_path)['main']['agent_id']),
                        environment={}, expected_model='model', spend_limit=Decimal(1),
                        authorized=True)
    assert got['status'] == 'blocked'
    assert got['error_code'] == 'pool_spend_limit'


def test_saved_request_tampering_is_rejected(tmp_path, monkeypatch):
    make_pool(tmp_path)
    fake_boundary(monkeypatch)
    request = req(tmp_path)
    got = turn(tmp_path, request)
    path = Path(got['records_path']) / 'request.json'
    saved = loads(path.read_text())
    saved['direct_session'] = False
    path.write_bytes(json_bytes(saved))
    with pytest.raises(ValueError, match='pool_request_changed'):
        turn(tmp_path, request)


def test_wrong_session_id_is_rejected_before_launch(tmp_path):
    make_pool(tmp_path)
    with pytest.raises(ValueError, match='pool_request_invalid'):
        turn(tmp_path, replace(req(tmp_path), session_id=str(uuid4())))


def test_unmanaged_session_blocks_spend(tmp_path):
    make_pool(tmp_path)
    (tmp_path / 'sessions/pool-calls' / str(uuid4())).mkdir(parents=True)
    assert turn(tmp_path, req(tmp_path))['error_code'] == 'pool_unmanaged_call'


def test_missing_cost_blocks_future_spend(tmp_path, monkeypatch):
    make_pool(tmp_path)
    fake_boundary(monkeypatch)
    from app.analysis_execution import runner
    boundary = runner.run_process
    def unknown(*args, **kwargs):
        captured = boundary(*args, **kwargs)
        rows = [loads(line) for line in captured['stdout'].decode().splitlines()]
        rows[-1].pop('total_cost_usd')
        captured['stdout'] = b''.join(json_bytes(row) for row in rows)
        return captured
    monkeypatch.setattr(runner, 'run_process', unknown)
    got = turn(tmp_path, req(tmp_path))
    assert got['status'] == 'blocked'
    assert turn(tmp_path, req(tmp_path))['status'] == 'blocked'


def test_pool_member_names_allow_application_role_underscores(tmp_path):
    from app.analysis_execution.session_pool import create_session_pool
    pool = create_session_pool(tmp_path, video_id='BV123', model='model', provider='test',
                               members=[{'member_id': 'worker-user_initial',
                                         'role': 'user_initial', 'role_sha256': 'a' * 64}])
    assert pool['members'][0]['member_id'] == 'worker-user_initial'


def test_cost_accepts_unicode_line_separator_inside_json_string():
    from app.analysis_execution.session_pool import _cost
    session_id = str(uuid4())
    raw = json_bytes({'type': 'result', 'session_id': session_id,
                      'total_cost_usd': Decimal('.05'), 'result': 'first\u2028second\u2029third'})
    assert _cost(raw, session_id) == Decimal('.05')
