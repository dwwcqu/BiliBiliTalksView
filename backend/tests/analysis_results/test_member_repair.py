"""Recovery preserves native births and never repeats repair attempts."""
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from test_initialization import (
    MEMBERS,
    initialize,
    install_creation_boundary,
    setup_initialization,
    uncreated_case,
)

from app.analysis_execution import runner
from app.analysis_execution.initialization import load_team
from app.analysis_packets.codec import json_bytes, loads


def pending(output_case, monkeypatch, damage=None, members=None):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, request)
    original = runner.run_process

    def boundary(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(calls) == 2:
            events = [loads(line) for line in result['stdout'].decode().splitlines()]
            events[-2]['summary'] = '{"wrong":true}'
            events[-1].update(subtype='error_max_budget_usd', is_error=True)
            if damage == 'prompt':
                events[1]['message']['content'][0]['input']['prompt'] = '{}'
            elif damage == 'unclosed':
                events.pop(-2)
            elif damage == 'id':
                events[-2]['task_id'] = 'invented-id'
            elif damage in ('correction', 'correction_live', 'correction_wrong_id'):
                agent = 'native-context'
                target = 'invented-id' if damage == 'correction_wrong_id' else agent
                common = {'session_id': request.session_id}
                correction = [
                    {'type': 'assistant', 'message': {'content': [
                        {'type': 'tool_use', 'id': 'old-correction', 'name': 'SendMessage',
                         'input': {'to': target, 'message': 'Correct JSON'}}]}},
                    {'type': 'system', 'subtype': 'task_started', 'task_id': agent,
                     'tool_use_id': 'old-correction', 'spawn_depth': 1,
                     'task_type': 'local_agent'},
                    {'type': 'user', 'message': {'content': [
                        {'type': 'tool_result', 'tool_use_id': 'old-correction', 'content': [
                            {'type': 'text', 'text': json_bytes({'success': True,
                             'resumedAgentId': agent}).decode()}]}]}},
                    {'type': 'system', 'subtype': 'task_notification', 'task_id': agent,
                     'tool_use_id': 'old-correction', 'status': 'stopped', 'summary': 'budget'},
                ]
                if damage == 'correction_live':
                    correction.pop()
                events[-1:-1] = [e | common for e in correction]
            elif damage == 'cost':
                events[-1].pop('total_cost_usd')
            result.update(stdout=b''.join(json_bytes(e) for e in events), returncode=1)
        return result

    monkeypatch.setattr(runner, 'run_process', boundary)
    assert initialize(case, request, members=members, spend_limit=Decimal(1))['status'] == 'blocked'
    return case, request, calls


def repair_boundary(monkeypatch, request, *, fail=False, unknown=False):
    calls = []

    def boundary(argv, **kwargs):
        calls.append(argv)
        assert 'Read,SendMessage' in argv
        assert '--bare' not in argv
        assert kwargs['env']['CLAUDE_CONFIG_DIR'] == str(request.cwd / 'sessions/claude-state')
        repair = next((request.cwd / 'sessions/team/members/context/repairs').glob('*/intent.json'))
        intent = loads(repair.read_text(encoding='utf-8'))
        assert kwargs['stdin'].decode() == intent['prompt']
        message = loads(intent['message'])
        assert intent['agent_id'] == 'native-context'
        common = {'session_id': request.session_id}
        events = [
            {'type': 'system', 'subtype': 'init', 'model': 'synthetic-reported',
             'claude_code_version': '2.1.261', 'cwd': str(request.cwd)},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'repair-call', 'name': 'SendMessage',
                 'input': {'to': intent['agent_id'], 'message': intent['message'],
                           'summary': 'Repair initialization handshake'}}]}},
            {'type': 'system', 'subtype': 'task_started', 'task_id': intent['agent_id'],
             'tool_use_id': 'repair-call', 'spawn_depth': 1, 'task_type': 'local_agent'},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'repair-call', 'content': [
                    {'type': 'text', 'text': json_bytes({'success': True,
                     'resumedAgentId': intent['agent_id']}).decode()}]}]}},
            {'type': 'system', 'subtype': 'task_notification', 'task_id': intent['agent_id'],
             'tool_use_id': 'repair-call', 'status': 'completed',
             'summary': json_bytes(message['required_response']).decode()},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'result': 'repaired', 'total_cost_usd': Decimal('0.02')},
        ]
        if fail:
            events[-1].update(subtype='error_max_budget_usd', is_error=True)
        if unknown:
            events[-1].pop('total_cost_usd')
        return {'stdout': b''.join(json_bytes(e | common) for e in events),
                'stderr': b'', 'returncode': 1 if fail else 0, 'stop_reason': None}

    monkeypatch.setattr(runner, 'run_process', boundary)
    return calls


def run_repair(request, **kwargs):
    from app.analysis_execution.member_repair import repair_member_handshake
    return repair_member_handshake(request.cwd, 'context', request=request,
                                   environment={}, spend_limit=Decimal(1), authorized=True,
                                   **kwargs)


def test_repair_same_child_once_and_initialize_counts_cost(output_case, monkeypatch):
    case, original, creations = pending(output_case, monkeypatch)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    calls = repair_boundary(monkeypatch, request)
    result = run_repair(request)
    assert result['status'] == 'repaired'
    assert result['agent_id'] == 'native-context'
    assert run_repair(request)['reused'] is True
    assert len(calls) == 1
    team = initialize(case, original, spend_limit=Decimal(1))
    assert team['status'] == 'initialized'
    assert team['actual_estimated_cost_usd'] == '0.04'
    assert team['acknowledgement_repaired'] is True
    assert len(creations) == 2 and len(calls) == 1
    assert load_team(request.cwd)['members'] == team['members']
    proof = request.cwd / 'sessions/team' / result['proof_ref']['path']
    proof.write_bytes(b'{}')
    with pytest.raises(ValueError):
        load_team(request.cwd)


@pytest.mark.parametrize('damage', ['prompt', 'id', 'unclosed', 'cost',
                                    'correction_live', 'correction_wrong_id'])
def test_invalid_original_never_launches(output_case, monkeypatch, damage):
    _, original, _ = pending(output_case, monkeypatch, damage)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    calls = repair_boundary(monkeypatch, request)
    code = {'prompt': 'initialization_message_mismatch', 'id': 'task_identity_mismatch',
            'unclosed': 'missing_creation_evidence', 'cost': 'initialization_cost_unknown',
            'correction_live': 'missing_creation_evidence',
            'correction_wrong_id': 'correction_recipient_mismatch'}[damage]
    with pytest.raises(ValueError, match=code):
        run_repair(request)
    assert not calls


@pytest.mark.parametrize('unknown', [False, True])
def test_failed_repair_never_replayed_or_followed(output_case, monkeypatch, unknown):
    case, original, _ = pending(output_case, monkeypatch)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    calls = repair_boundary(monkeypatch, request, fail=True, unknown=unknown)
    assert run_repair(request)['status'] == 'blocked'
    assert run_repair(request)['status'] == 'blocked'
    with pytest.raises(ValueError, match='repair_history_incomplete'):
        run_repair(replace(request, attempt_id=str(uuid4())))
    assert initialize(case, original, spend_limit=Decimal(1))['error_code'] == (
        'repair_history_incomplete')
    assert len(calls) == 1




def test_closed_old_correction_is_management_only(output_case, monkeypatch):
    from app.analysis_execution.member_repair import inspect_pending_birth
    case, original, _ = pending(output_case, monkeypatch, 'correction')
    base = original.cwd / 'sessions/team'
    intent = loads((base / 'intent.json').read_text(encoding='utf-8'))
    step = intent['steps'][1]
    record = original.cwd / 'sessions/cli-calls' / original.session_id / step['attempt_id']
    data = {k: (record / name).read_bytes() for k, name in
            {'request': 'request.json', 'outcome': 'outcome.json', 'stdout': 'stdout.jsonl'}.items()}
    birth = inspect_pending_birth(intent, step, data)
    assert birth['handshake_verified'] is False
    assert birth['agent_id'] == 'native-context'
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    repair_boundary(monkeypatch, request)
    assert run_repair(request)['status'] == 'repaired'
    assert initialize(case, original, spend_limit=Decimal(1))['status'] == 'initialized'


def test_repair_continues_only_uncreated_third_member(output_case, monkeypatch):
    members = MEMBERS + [{'member_id': 'synth', 'role': 'user_synthesis'}]
    case, original, creations = pending(output_case, monkeypatch, members=members)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    repairs = repair_boundary(monkeypatch, request)
    assert run_repair(request)['status'] == 'repaired'
    final = install_creation_boundary(monkeypatch, original)
    team = initialize(case, original, members=members, spend_limit=Decimal(1))
    assert team['status'] == 'initialized'
    assert team['actual_estimated_cost_usd'] == '0.05'
    assert len(creations) == 2 and len(repairs) == 1
    assert [s['member_id'] for s in final] == ['synth']


def test_repair_reserves_remaining_original_and_empirical_headroom(output_case, monkeypatch):
    from app.analysis_execution.member_repair import repair_member_handshake
    _, original, _ = pending(output_case, monkeypatch)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    calls = repair_boundary(monkeypatch, request)
    with pytest.raises(ValueError, match='blocked_budget_headroom'):
        repair_member_handshake(request.cwd, 'context', request=request, environment={},
                                spend_limit=Decimal('0.06'), authorized=True)
    assert not calls


def test_already_valid_original_reuses_proof_without_launch(output_case, monkeypatch):
    case = uncreated_case(output_case)
    original = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, original)
    initialize(case, original)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    result = run_repair(request)
    assert result['reused'] is True
    assert result['proof_ref']['path'] == 'members/context/proof.json'
    assert len(calls) == 2


def test_completed_repair_recovers_pointer_without_relaunch(output_case, monkeypatch):
    from app.analysis_execution import initialization as module
    _, original, _ = pending(output_case, monkeypatch)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    calls = repair_boundary(monkeypatch, request)
    save = module._save

    def crash(base, path, raw):
        if path == 'members/context/repair_ref.json':
            raise OSError('crash before pointer')
        return save(base, path, raw)

    monkeypatch.setattr(module, '_save', crash)
    with pytest.raises(OSError, match='crash before pointer'):
        run_repair(request)
    monkeypatch.setattr(module, '_save', save)
    with pytest.raises(ValueError, match='repair_intent_conflict'):
        run_repair(replace(request, budget_usd=Decimal('0.04')))
    result = run_repair(request)
    assert result['status'] == 'repaired'
    assert result['reused'] is True
    assert len(calls) == 1


def refusal_boundary(monkeypatch, request, *, damage=None):
    calls = repair_boundary(monkeypatch, request)
    boundary = runner.run_process

    def refused(*args, **kwargs):
        result = boundary(*args, **kwargs)
        events = [loads(line) for line in result['stdout'].decode().splitlines()]
        started, notification = events[2], events[4]
        refused_result = events[3]
        refused_result['message']['content'][0]['content'][0]['text'] = json_bytes({
            'success': False, 'message': 'Agent was stopped; it will not be resumed.'}).decode()
        if damage == 'tool':
            refused_result['message']['content'][0]['tool_use_id'] = 'unrelated'
        if damage == 'id':
            events[1]['message']['content'][0]['input']['to'] = 'invented-id'
        if damage == 'message':
            events[1]['message']['content'][0]['input']['message'] = '{}'
        historical = notification | {'status': 'stopped', 'tool_use_id': None, 'summary': 'old'}
        leading = events[-1] | {'total_cost_usd': 0}
        assert started['task_id'] == 'native-context'
        events = [historical, events[0], leading, events[0], events[1], refused_result, events[-1]]
        result['stdout'] = b''.join(json_bytes(e) for e in events)
        return result

    monkeypatch.setattr(runner, 'run_process', refused)
    return calls


def test_native_refusal_overrides_historical_notification_without_success(output_case, monkeypatch):
    from app.analysis_execution.member_repair import repair_failure
    case, original, _ = pending(output_case, monkeypatch)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    calls = refusal_boundary(monkeypatch, request)
    result = run_repair(request)
    assert result['status'] == 'blocked'
    assert result['error_code'] == 'member_resume_refused'
    assert result['actual_estimated_cost_usd'] == '0.02'
    assert run_repair(request)['error_code'] == 'member_resume_refused'
    assert len(calls) == 1
    base = original.cwd / 'sessions/team'
    intent = loads((base / 'intent.json').read_text(encoding='utf-8'))
    assert repair_failure(base, intent, intent['steps'][1]) == {
        'error_code': 'member_resume_refused', 'agent_id': 'native-context', 'cost': Decimal('0.02')}
    assert not (base / 'members/context/repair_ref.json').exists()
    assert not (base / 'members/context/repairs' / request.attempt_id / 'proof.json').exists()
    assert initialize(case, original, spend_limit=Decimal(1))['status'] == 'blocked'
    copied = base / 'members/context/repairs' / request.attempt_id / 'stdout.jsonl'
    copied.write_bytes(b'{}\n')
    with pytest.raises(ValueError, match='repair_transport_failed'):
        repair_failure(base, intent, intent['steps'][1])


@pytest.mark.parametrize('damage', ['tool', 'id', 'message'])
def test_uncorrelated_refusal_is_not_trusted(output_case, monkeypatch, damage):
    _, original, _ = pending(output_case, monkeypatch)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    refusal_boundary(monkeypatch, request, damage=damage)
    assert run_repair(request)['error_code'] != 'member_resume_refused'


@pytest.mark.parametrize('sequence,accepted', [
    ([0, 'tool', Decimal('0.12')], True),
    ([0, 0, 'tool', Decimal('0.12'), Decimal('0.12')], True),
    (['tool', 0, Decimal('0.12')], False),
    ([0, 'tool', Decimal('0.12'), 0], False),
    ([0, 'tool', Decimal('0.12'), Decimal('0.13')], False),
])
def test_startup_zero_cost_only_before_all_tool_activity(sequence, accepted):
    from app.analysis_execution.initialization import _cost
    events = [({'type': 'assistant', 'message': {'content': [{'type': 'tool_use'}]}}
               if value == 'tool' else {'type': 'result', 'session_id': 'main',
                                        'total_cost_usd': value}) for value in sequence]
    raw = b''.join(json_bytes(e) for e in events)
    if accepted:
        assert _cost(raw, 'main') == Decimal('0.12')
    else:
        with pytest.raises(ValueError, match='initialization_cost_unknown'):
            _cost(raw, 'main')


@pytest.mark.parametrize('refused,canonical_bad,expected', [
    (True, False, 'member_resume_refused'),
    (True, True, 'delivery_message_mismatch'),
    (False, False, 'delivery_message_mismatch'),
])
def test_truncated_legacy_content_only_ignored_for_correlated_refusal(
        output_case, monkeypatch, refused, canonical_bad, expected):
    _, original, _ = pending(output_case, monkeypatch)
    request = replace(original, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    calls = (refusal_boundary if refused else repair_boundary)(monkeypatch, request)
    boundary = runner.run_process

    def truncated(*args, **kwargs):
        result = boundary(*args, **kwargs)
        events = [loads(line) for line in result['stdout'].decode().splitlines()]
        for event in events:
            for block in event.get('message', {}).get('content', []):
                if block.get('name') == 'SendMessage':
                    block['input']['content'] = '{"operation":"repair_initialization_handshake","r...'
                    if canonical_bad:
                        block['input']['message'] = '{}'
        result['stdout'] = b''.join(json_bytes(e) for e in events)
        return result

    monkeypatch.setattr(runner, 'run_process', truncated)
    result = run_repair(request)
    assert result['status'] == 'blocked'
    assert result['error_code'] == expected
    assert len(calls) == 1
    assert not (request.cwd / 'sessions/team/members/context/repair_ref.json').exists()
