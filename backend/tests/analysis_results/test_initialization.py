"""Synthetic process boundary verifies immutable native team initialization."""

import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.analysis_execution.command import CliRequest
from app.analysis_execution.initialization import initialize_team, load_team
from app.analysis_packets.builder import build_primary
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_packets.publication import publish_assembly
from app.analysis_results.acceptance import bind_output_schema

MEMBERS = [{'member_id': 'reader', 'role': 'user_initial'},
           {'member_id': 'context', 'role': 'thread_context'}]


def uncreated_case(case):
    assembly = bind_output_schema(build_primary(
        case['bundle'], str(uuid4()), case['assembly'].resources, case['budget'],
        resource_files=case['assembly'].resource_files))
    return case | {'assembly': assembly,
                   'publication': publish_assembly(case['root'], assembly, case['bundle'])}


def setup_initialization(case):
    return CliRequest(Path(sys.executable), Path(case['publication']['run_path']).parent.parent,
                      str(uuid4()), str(uuid4()), 'synthetic-model', 'unused', Decimal('0.1'),
                      5, 1000000, False)


def initialize(case, request, members=None, **kwargs):
    return initialize_team(case['root'], case['assembly'].run_id,
                           case['publication']['manifest_id'], request=request,
                           members=members or MEMBERS, rule_catalog=case['rules'],
                           provider='synthetic-provider', expected_model='synthetic-reported',
                           environment={}, authorized=True, **kwargs)


def install_creation_boundary(monkeypatch, request):
    from app.analysis_execution import runner
    calls = []

    def boundary(argv, **kwargs):
        intent = loads((request.cwd / 'sessions/team/intent.json').read_text(encoding='utf-8'))
        step = next(s for s in intent['steps'] if s['prompt'].encode() == kwargs['stdin'])
        assert kwargs['env']['CLAUDE_CONFIG_DIR'] == str(request.cwd / 'sessions/claude-state')
        calls.append(step)
        agent, call = 'native-' + step['member_id'], 'call-' + step['member_id']
        common = {'session_id': request.session_id}
        summary = json_bytes({k: step[k] for k in
                              ('member_id', 'role', 'initialization_sha256')}).decode()
        events = [
            {'type': 'system', 'subtype': 'init', 'model': 'synthetic-reported',
             'claude_code_version': '2.1.261', 'cwd': str(request.cwd)},
            {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'content': [
                {'type': 'tool_use', 'id': call, 'name': 'Agent',
                 'input': {'subagent_type': step['member_id'], 'prompt': step['message'],
                           'run_in_background': False}}]}},
            {'type': 'system', 'subtype': 'task_started', 'task_id': agent,
             'tool_use_id': call, 'spawn_depth': 1, 'task_type': 'local_agent',
             'subagent_type': step['member_id']},
            {'type': 'user', 'parent_tool_use_id': None, 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': call,
                 'content': [{'type': 'text', 'text': 'completed'}]}]}},
            {'type': 'system', 'subtype': 'task_notification', 'task_id': agent,
             'tool_use_id': call, 'status': 'completed', 'summary': summary},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'result': 'initialized',
             'total_cost_usd': min(Decimal('0.01'), Decimal(step['budget_usd']))},
        ]
        return {'stdout': b''.join(json_bytes(e | common) for e in events),
                'stderr': b'', 'returncode': 0, 'stop_reason': None}

    monkeypatch.setattr(runner, 'run_process', boundary)
    return calls


def test_native_team_is_committed_and_reused(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, request)
    team = initialize(case, request)
    assert team['status'] == 'initialized', team
    assert [m['agent_id'] for m in team['members']] == ['native-reader', 'native-context']
    assert [s['resume'] for s in calls] == [False, True]
    assert sum(Decimal(s['budget_usd']) for s in calls) == Decimal('0.1')
    assert load_team(request.cwd)['members'] == team['members']
    assert initialize(case, replace(request, attempt_id=str(uuid4()), session_id=str(uuid4())))['reused']
    assert len(calls) == 2


def test_registered_origin_cannot_create_duplicate_team(output_case, monkeypatch):
    request = setup_initialization(output_case)
    calls = install_creation_boundary(monkeypatch, request)
    with pytest.raises(ValueError, match='team_registry_conflict'):
        initialize(output_case, request)
    assert calls == []


def test_crash_before_team_commit_reuses_completed_records(output_case, monkeypatch):
    from app.analysis_execution import initialization as module
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, request)
    original = module._save

    def crash(base, path, raw):
        if path == 'team.json':
            raise OSError('synthetic crash')
        return original(base, path, raw)

    monkeypatch.setattr(module, '_save', crash)
    with pytest.raises(OSError, match='synthetic crash'):
        initialize(case, request)
    monkeypatch.setattr(module, '_save', original)
    assert initialize(case, request)['status'] == 'initialized'
    assert len(calls) == 2


def test_intent_conflict_blocks_attempt_replacement(output_case, monkeypatch):
    from app.analysis_execution import runner
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    monkeypatch.setattr(runner, 'run_process', lambda *a, **k: {
        'stdout': b'', 'stderr': b'', 'returncode': 1, 'stop_reason': None})
    assert initialize(case, request)['status'] == 'blocked'
    with pytest.raises(ValueError, match='initialization_intent_conflict'):
        initialize(case, replace(request, attempt_id=str(uuid4())))
    assert initialize(case, request)['status'] == 'blocked'


def test_tampered_proof_fails_loading(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    install_creation_boundary(monkeypatch, request)
    initialize(case, request)
    (request.cwd / 'sessions/team/members/reader/stdout.jsonl').write_bytes(b'{}')
    with pytest.raises(ValueError, match='team_hash_mismatch'):
        load_team(request.cwd)




def test_missing_outcome_blocks_without_replay(output_case, monkeypatch):
    from app.analysis_execution import runner
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = []

    def crash(*args, **kwargs):
        calls.append(1)
        raise OSError('process interrupted')

    monkeypatch.setattr(runner, 'run_process', crash)
    with pytest.raises(OSError, match='process interrupted'):
        initialize(case, request)
    assert initialize(case, request)['status'] == 'blocked'
    assert len(calls) == 1


def test_changed_native_configuration_cannot_reuse_team(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, request)
    initialize(case, request)
    with pytest.raises(ValueError, match='team_configuration_conflict'):
        initialize(case, replace(request, model='another-model'))
    with pytest.raises(ValueError, match='team_configuration_conflict'):
        initialize(case, request, members=[MEMBERS[0]])
    assert len(calls) == 2


def test_budget_reservations_round_down(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = replace(setup_initialization(case), budget_usd=Decimal('0.000003'))
    calls = install_creation_boundary(monkeypatch, request)
    assert initialize(case, request)['status'] == 'initialized'
    assert [step['budget_usd'] for step in calls] == ['0.000001', '0.000001']


def test_request_and_proof_are_validated_on_completed_replay(output_case, monkeypatch):
    from app.analysis_execution import initialization as module
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, request)
    original = module._save

    def crash(base, path, raw):
        if path == 'team.json':
            raise OSError('synthetic crash')
        return original(base, path, raw)

    monkeypatch.setattr(module, '_save', crash)
    with pytest.raises(OSError):
        initialize(case, request)
    monkeypatch.setattr(module, '_save', original)
    intent = loads((request.cwd / 'sessions/team/intent.json').read_text(encoding='utf-8'))
    record = (request.cwd / 'sessions/cli-calls' / request.session_id
              / intent['steps'][0]['attempt_id'] / 'request.json')
    saved = loads(record.read_text(encoding='utf-8'))
    record.write_bytes(json_bytes(saved | {'agents_sha256': '0' * 64}))
    assert initialize(case, request)['error_code'] == 'transport_request_changed'
    assert len(calls) == 2


def test_unauthorized_and_conflicting_environment_write_no_intent(output_case):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    kwargs = {'request': request, 'members': MEMBERS, 'rule_catalog': case['rules'],
              'provider': 'synthetic-provider', 'expected_model': 'synthetic-reported'}
    with pytest.raises(ValueError, match='execution_not_authorized'):
        initialize_team(case['root'], case['assembly'].run_id,
                        case['publication']['manifest_id'], environment={}, **kwargs)
    with pytest.raises(ValueError, match='cli_state_dir_conflict'):
        initialize_team(case['root'], case['assembly'].run_id,
                        case['publication']['manifest_id'], authorized=True,
                        environment={'CLAUDE_CONFIG_DIR': 'other'}, **kwargs)
    assert not (request.cwd / 'sessions/team/intent.json').exists()



def test_wrong_saved_cli_state_blocks_recovery(output_case, monkeypatch):
    from app.analysis_execution import initialization as module
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, request)
    original = module._save

    def crash(base, path, raw):
        if path == 'team.json':
            raise OSError('synthetic crash')
        return original(base, path, raw)

    monkeypatch.setattr(module, '_save', crash)
    with pytest.raises(OSError):
        initialize(case, request)
    monkeypatch.setattr(module, '_save', original)
    intent = loads((request.cwd / 'sessions/team/intent.json').read_text(encoding='utf-8'))
    record = (request.cwd / 'sessions/cli-calls' / request.session_id
              / intent['steps'][0]['attempt_id'] / 'request.json')
    saved = loads(record.read_text(encoding='utf-8'))
    record.write_bytes(json_bytes(saved | {'cli_state_dir': 'wrong-state'}))
    assert initialize(case, request)['error_code'] == 'transport_request_changed'
    assert len(calls) == 2


def test_team_records_main_only_model_provenance(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, request)
    team = initialize(case, request)
    assert team['reported_model_scope'] == 'main_session_only'
    assert team['member_models_verified'] is False
    assert 'omit model' in calls[0]['prompt']
    path = request.cwd / 'sessions/team/team.json'
    saved = loads(path.read_text(encoding='utf-8'))
    path.write_bytes(json_bytes(saved | {'member_models_verified': True}))
    with pytest.raises(ValueError, match='team_identity_mismatch'):
        load_team(request.cwd)


def empty_first(case, request, monkeypatch, extra=None):
    from app.analysis_execution import runner
    events = [
        {'type': 'system', 'subtype': 'init', 'session_id': request.session_id,
         'model': 'synthetic-reported', 'claude_code_version': '2.1.261',
         'cwd': str(request.cwd)},
        *([extra] if extra else []),
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'session_id': request.session_id, 'result': 'cannot create', 'total_cost_usd': 0},
    ]
    monkeypatch.setattr(runner, 'run_process', lambda *a, **k: {
        'stdout': b''.join(json_bytes(e) for e in events), 'stderr': b'',
        'returncode': 0, 'stop_reason': None})
    assert initialize(case, request)['status'] == 'blocked'


def test_fresh_initialization_uses_managed_creation(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    install_creation_boundary(monkeypatch, request)
    initialize(case, request)
    intent = loads((request.cwd / 'sessions/team/intent.json').read_text(encoding='utf-8'))
    assert intent['create_agent'] is True
    assert intent['bare'] is False


@pytest.mark.parametrize('legacy', [False, True])
def test_empty_first_recovery_preserves_main(output_case, monkeypatch, legacy):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    empty_first(case, request, monkeypatch)
    if legacy:
        intent_path = request.cwd / 'sessions/team/intent.json'
        old = loads(intent_path.read_text(encoding='utf-8'))
        for key in ('create_agent', 'bare', 'first_step_resume'):
            old.pop(key)
        intent_path.write_bytes(json_bytes(old))
        record = (request.cwd / 'sessions/cli-calls' / request.session_id
                  / old['steps'][0]['attempt_id'] / 'request.json')
        saved = loads(record.read_text(encoding='utf-8'))
        saved.pop('create_agent')
        saved.pop('bare')
        record.write_bytes(json_bytes(saved))
    calls = install_creation_boundary(monkeypatch, request)
    recovered = replace(request, attempt_id=str(uuid4()))
    team = initialize(case, recovered, recover_empty=True)
    assert team['session_id'] == request.session_id
    assert [s['resume'] for s in calls] == [True, True]
    assert initialize(case, recovered, recover_empty=True)['reused']
    assert len(calls) == 2
    history = request.cwd / 'sessions/team/history' / request.attempt_id / 'stdout.jsonl'
    history.write_bytes(b'{}')
    with pytest.raises(ValueError):
        load_team(request.cwd)


@pytest.mark.parametrize('extra', [
    {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}},
    {'type': 'user', 'message': {'content': [{'type': 'tool_result'}]}},
    {'type': 'system', 'subtype': 'task_started'},
    {'type': 'system', 'subtype': 'task_notification'},
])
def test_empty_recovery_rejects_native_activity(output_case, monkeypatch, extra):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    empty_first(case, request, monkeypatch, extra | {'session_id': request.session_id})
    with pytest.raises(ValueError):
        initialize(case, replace(request, attempt_id=str(uuid4())), recover_empty=True)


@pytest.mark.parametrize('damage', ['missing', 'malformed', 'session'])
def test_empty_recovery_rejects_incomplete_identity(output_case, monkeypatch, damage):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    empty_first(case, request, monkeypatch)
    intent = loads((request.cwd / 'sessions/team/intent.json').read_text(encoding='utf-8'))
    outcome = (request.cwd / 'sessions/cli-calls' / request.session_id
               / intent['steps'][0]['attempt_id'] / 'outcome.json')
    if damage == 'missing':
        outcome.unlink()
    elif damage == 'malformed':
        outcome.write_bytes(b'{}')
    recovered = replace(request, attempt_id=str(uuid4()),
                        session_id=str(uuid4()) if damage == 'session' else request.session_id)
    with pytest.raises(ValueError):
        initialize(case, recovered, recover_empty=True)


@pytest.mark.parametrize('damage', ['later', 'proof', 'mode', 'prompt', 'definitions'])
def test_empty_recovery_rejects_conflicting_records(output_case, monkeypatch, damage):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    empty_first(case, request, monkeypatch)
    intent = loads((request.cwd / 'sessions/team/intent.json').read_text(encoding='utf-8'))
    records = request.cwd / 'sessions/cli-calls' / request.session_id
    if damage == 'later':
        (records / intent['steps'][1]['attempt_id']).mkdir()
    elif damage == 'proof':
        proof = request.cwd / 'sessions/team/members/reader'
        proof.mkdir(parents=True)
        (proof / 'proof.json').write_bytes(b'{}')
    else:
        record = records / intent['steps'][0]['attempt_id'] / 'request.json'
        saved = loads(record.read_text(encoding='utf-8'))
        if damage == 'mode':
            saved.pop('bare')
        else:
            saved['prompt_sha256' if damage == 'prompt' else 'agents_sha256'] = '0' * 64
        record.write_bytes(json_bytes(saved))
    with pytest.raises(ValueError):
        initialize(case, replace(request, attempt_id=str(uuid4())), recover_empty=True)


@pytest.mark.parametrize('change', ['budget', 'session', 'model', 'members'])
def test_recovery_retry_rejects_changed_request(output_case, monkeypatch, change):
    from app.analysis_execution import runner
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    empty_first(case, request, monkeypatch)
    recovered = replace(request, attempt_id=str(uuid4()))
    monkeypatch.setattr(runner, 'run_process', lambda *a, **k: {
        'stdout': b'', 'stderr': b'', 'returncode': 1, 'stop_reason': None})
    assert initialize(case, recovered, recover_empty=True)['status'] == 'blocked'
    changed = replace(recovered, **{
        'budget': {'budget_usd': Decimal('0.001')},
        'session': {'session_id': str(uuid4())},
        'model': {'model': 'different-model'},
        'members': {},
    }[change])
    with pytest.raises(ValueError, match='initialization_intent_conflict'):
        initialize(case, changed, members=[MEMBERS[0]] if change == 'members' else None,
                   recover_empty=True)
    with pytest.raises(ValueError, match='initialization_intent_conflict'):
        initialize(case, recovered)


def test_interrupted_recovery_resumes_exact_frozen_request(output_case, monkeypatch):
    from app.analysis_execution import initialization as module
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    empty_first(case, request, monkeypatch)
    recovered = replace(request, attempt_id=str(uuid4()))
    original = module.execute

    def interrupt(*args, **kwargs):
        raise OSError('interrupted before launch')

    monkeypatch.setattr(module, 'execute', interrupt)
    with pytest.raises(OSError, match='interrupted before launch'):
        initialize(case, recovered, recover_empty=True)
    monkeypatch.setattr(module, 'execute', original)
    calls = install_creation_boundary(monkeypatch, request)
    team = initialize(case, recovered, recover_empty=True)
    assert team['status'] == 'initialized'
    assert [step['resume'] for step in calls] == [True, True]


def install_budget_boundary(monkeypatch, request, *, cost=Decimal('0.08'), mixed=False,
                            partial=False):
    from app.analysis_execution import runner
    calls = install_creation_boundary(monkeypatch, request)
    original = runner.run_process

    def boundary(*args, **kwargs):
        captured = original(*args, **kwargs)
        events = [loads(line) for line in captured['stdout'].decode().splitlines()]
        if len(calls) == 1:
            events[-1].update(subtype='error_max_budget_usd', is_error=True)
            if cost is None:
                events[-1].pop('total_cost_usd')
            else:
                events[-1]['total_cost_usd'] = cost
            if partial:
                events = [e for e in events if e.get('subtype') != 'task_notification']
            if mixed:
                events.append(events[-1] | {'total_cost_usd': Decimal('0.09')})
            captured['stdout'] = b''.join(json_bytes(e) for e in events)
        return captured

    monkeypatch.setattr(runner, 'run_process', boundary)
    return calls


def test_budget_stopped_identity_preserved_and_explicit_limit_resumes(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_budget_boundary(monkeypatch, request)
    blocked = initialize(case, request)
    assert blocked['status'] == 'blocked'
    assert blocked['error_code'] == 'blocked_budget_headroom'
    proof = request.cwd / 'sessions/team/members/reader/proof.json'
    saved = loads(proof.read_text(encoding='utf-8'))
    assert saved['native']['parent_budget_stopped'] is True
    assert len(calls) == 1
    team = initialize(case, request, spend_limit=Decimal('0.40'))
    assert team['status'] == 'initialized'
    assert team['actual_estimated_cost_usd'] == '0.09'
    assert team['parent_budget_stopped'] is True
    assert len(calls) == 2
    assert load_team(request.cwd)['members'] == team['members']


@pytest.mark.parametrize('cost,mixed,partial', [
    (None, False, False), (Decimal('0.08'), True, False),
    (Decimal('0.08'), False, True), (Decimal('0.12'), False, False),
])
def test_budget_stop_cannot_hide_unknown_cost_or_partial_proof(
        output_case, monkeypatch, cost, mixed, partial):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_budget_boundary(monkeypatch, request, cost=cost, mixed=mixed, partial=partial)
    assert initialize(case, request)['status'] == 'blocked'
    assert len(calls) == 1
    assert not (request.cwd / 'sessions/team/team.json').exists()
    if not partial:
        assert (request.cwd / 'sessions/team/members/reader/proof.json').exists()


@pytest.mark.parametrize('returncode', [True, 2, -1])
def test_budget_stop_rejects_other_process_exits(output_case, monkeypatch, returncode):
    from app.analysis_execution import runner
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_budget_boundary(monkeypatch, request)
    original = runner.run_process
    monkeypatch.setattr(runner, 'run_process', lambda *a, **k: original(*a, **k)
                        | {'returncode': returncode})
    assert initialize(case, request)['status'] == 'blocked'
    assert len(calls) == 1
    assert not (request.cwd / 'sessions/team/members/reader/proof.json').exists()


def test_spending_authorization_and_measured_total_are_revalidated(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    install_creation_boundary(monkeypatch, request)
    team = initialize(case, request, spend_limit=Decimal('0.40'))
    path = request.cwd / 'sessions/team/team.json'
    original = path.read_bytes()
    path.write_bytes(json_bytes(team | {'actual_estimated_cost_usd': '0'}))
    with pytest.raises(ValueError, match='initialization_spend_invalid'):
        load_team(request.cwd)
    path.write_bytes(original)
    auth = request.cwd / 'sessions/team' / team['spending_authorization_ref']['path']
    auth.write_bytes(b'{}')
    with pytest.raises(ValueError, match='team_hash_mismatch'):
        load_team(request.cwd)


def test_repeated_identical_budget_cost_is_counted_once(output_case, monkeypatch):
    from app.analysis_execution import runner
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_budget_boundary(monkeypatch, request)
    original = runner.run_process

    def repeat(*args, **kwargs):
        captured = original(*args, **kwargs)
        if len(calls) == 1:
            captured['stdout'] += captured['stdout'].splitlines(keepends=True)[-1]
        return captured

    monkeypatch.setattr(runner, 'run_process', repeat)
    team = initialize(case, request, spend_limit=Decimal('0.40'))
    assert team['status'] == 'initialized'
    assert team['actual_estimated_cost_usd'] == '0.09'
    assert len(calls) == 2


def test_overrun_continuation_requires_empirical_headroom(output_case, monkeypatch):
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_budget_boundary(monkeypatch, request)
    assert initialize(case, request)['status'] == 'blocked'
    blocked = initialize(case, request, spend_limit=Decimal('0.20'))
    assert blocked['status'] == 'blocked'
    assert blocked['error_code'] == 'blocked_budget_headroom'
    assert len(calls) == 1
    team = initialize(case, request, spend_limit=Decimal('0.40'))
    assert team['status'] == 'initialized'
    assert team['actual_estimated_cost_usd'] == '0.09'
    assert len(calls) == 2


@pytest.mark.parametrize('budget_stopped,cost,limit', [
    (False, Decimal('0.08'), Decimal('0.20')),
    (True, Decimal('0.04'), Decimal('0.10')),
])
def test_headroom_triggers_on_overquota_or_budget_stop(
        output_case, monkeypatch, budget_stopped, cost, limit):
    from app.analysis_execution import runner
    case = uncreated_case(output_case)
    request = setup_initialization(case)
    calls = install_budget_boundary(monkeypatch, request, cost=cost)
    original = runner.run_process

    def boundary(*args, **kwargs):
        captured = original(*args, **kwargs)
        if not budget_stopped:
            events = [loads(line) for line in captured['stdout'].decode().splitlines()]
            events[-1].update(subtype='success', is_error=False)
            captured['stdout'] = b''.join(json_bytes(e) for e in events)
        return captured

    monkeypatch.setattr(runner, 'run_process', boundary)
    result = initialize(case, request, spend_limit=limit)
    assert result['error_code'] == 'blocked_budget_headroom'
    assert len(calls) == 1
