"""Synthetic native CLI traces exercise the disk-backed dispatch boundary."""

import json
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from result_examples import register_members, response_for

from app.analysis_execution.command import CliRequest
from app.analysis_packets.codec import json_bytes


def setup_request(case):
    run = Path(case['publication']['run_path'])
    registry = case['assembly'].member_registry
    request = CliRequest(Path(sys.executable), run.parent.parent,
                         registry['main']['session_id'], str(uuid4()),
                         'synthetic-model', 'replaced by frozen delivery', Decimal('0.1'),
                         5, 1000000, True)
    config = {
        'run_id': case['assembly'].run_id, 'rules_sha256': case['rules']['rules_sha256'],
        'cli_version': '2.1.261', 'configured_model': request.model,
        'reported_model': 'synthetic-reported', 'provider': 'synthetic-provider',
        'session_id': request.session_id,
        'agent_ids': [m['agent_id'] for m in registry['members']],
        'input_protocol_version': '2.0.0', 'output_protocol_version': '1.0.0',
        'limits': case['assembly'].limits,
    }
    return request, config


def native_stream(delivery, config, request, *, wrong_digest=False):
    agent = delivery.member['agent_id']
    call = 'call-synthetic-dispatch'
    common = {'session_id': request.session_id}
    result = response_for(delivery.packet, request.attempt_id)
    summary = json_bytes({'delivery_sha256': '0'*64 if wrong_digest else delivery.delivery_sha256,
                          'task_result': result}).decode()
    events = [
        {'type': 'system', 'subtype': 'init', 'model': config['reported_model'],
         'claude_code_version': '2.1.261', 'cwd': str(request.cwd)},
        {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'content': [
            {'type': 'tool_use', 'id': call, 'name': 'SendMessage',
             'input': {'to': agent, 'message': delivery.message}}]}},
        {'type': 'system', 'subtype': 'task_started', 'task_id': agent,
         'tool_use_id': call, 'spawn_depth': 1, 'task_type': 'local_agent'},
        {'type': 'user', 'parent_tool_use_id': None, 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': call,
             'content': [{'type': 'text', 'text': json.dumps(
                 {'success': True, 'resumedAgentId': agent})}]}]}},
        {'type': 'system', 'subtype': 'task_notification', 'task_id': agent,
         'tool_use_id': call, 'status': 'completed', 'summary': summary},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'dispatched',
         'total_cost_usd': Decimal('0.02')},
    ]
    return b''.join(json_bytes(e | common) for e in events)


def dispatch(case, request, config, packet=None, **kwargs):
    from app.analysis_execution.dispatch import dispatch_task
    packet = packet or case['assembly'].packets[0]
    return dispatch_task(
        case['root'], case['assembly'].run_id, case['publication']['manifest_id'],
        packet['task_id'], request=request, agent_id='synthetic-' + packet['task_type'],
        rule_catalog=case['rules'], execution_config=config, environment={}, **kwargs)


def install_boundary(monkeypatch, case, request, config, *, wrong_digest=False, packet=None):
    from app.analysis_execution import runner
    from app.analysis_execution.delivery import prepare_delivery
    packet = packet or case['assembly'].packets[0]
    delivery = prepare_delivery(case['root'], case['assembly'].run_id,
                                case['publication']['manifest_id'], packet['task_id'],
                                request.attempt_id, 'synthetic-' + packet['task_type'], case['rules'])
    calls = []

    def boundary(argv, **kwargs):
        assert kwargs['stdin'] == delivery.prompt.encode('utf-8')
        assert argv[argv.index('--resume')+1] == request.session_id
        calls.append(1)
        return {'stdout': native_stream(delivery, config, request, wrong_digest=wrong_digest),
                'stderr': b'', 'returncode': 0, 'stop_reason': None}

    monkeypatch.setattr(runner, 'run_process', boundary)
    return calls


def test_dispatch_requires_authorization_before_writing(output_case):
    case = output_case
    request, config = setup_request(case)
    with pytest.raises(ValueError, match='execution_not_authorized'):
        dispatch(case, request, config)
    assert not (Path(case['publication']['run_path']) / 'execution.json').exists()


def test_verified_native_delivery_enters_real_acceptance(output_case, monkeypatch):
    case = output_case
    request, config = setup_request(case)
    calls = install_boundary(monkeypatch, case, request, config)
    outcome = dispatch(case, request, config, authorized=True)
    assert outcome['status'] == 'accepted'
    assert outcome['profile'] is None
    run = Path(case['publication']['run_path'])
    task_id = case['assembly'].packets[0]['task_id']
    assert (run / f'validation/{task_id}/accepted.json').is_file()
    assert (run / f'deliveries/{task_id}/{request.attempt_id}/delivery-proof.json').is_file()
    again = dispatch(case, request, config, authorized=True)
    assert again['status'] == 'accepted'
    assert len(calls) == 1


def test_wrong_rule_ack_never_enters_acceptance(output_case, monkeypatch):
    case = output_case
    request, config = setup_request(case)
    calls = install_boundary(monkeypatch, case, request, config, wrong_digest=True)
    outcome = dispatch(case, request, config, authorized=True)
    assert outcome['status'] == 'blocked'
    assert not list(Path(case['publication']['run_path']).glob('validation/*/accepted.json'))
    assert dispatch(case, request, config, authorized=True)['status'] == 'blocked'
    assert len(calls) == 1


def test_acceptance_interruption_resumes_without_model_replay(output_case, monkeypatch):
    from app.analysis_execution import dispatch as module
    case = output_case
    request, config = setup_request(case)
    calls = install_boundary(monkeypatch, case, request, config)
    original = module.accept_response

    def interrupted(*args, **kwargs):
        raise OSError('synthetic crash before receipt')

    monkeypatch.setattr(module, 'accept_response', interrupted)
    with pytest.raises(OSError):
        dispatch(case, request, config, authorized=True)
    monkeypatch.setattr(module, 'accept_response', original)
    assert dispatch(case, request, config, authorized=True)['status'] == 'accepted'
    assert len(calls) == 1


def test_native_proof_tampering_invalidates_catalog(output_case, monkeypatch):
    from app.analysis_results.acceptance import load_catalog
    case = output_case
    request, config = setup_request(case)
    install_boundary(monkeypatch, case, request, config)
    assert dispatch(case, request, config, authorized=True)['status'] == 'accepted'
    run = Path(case['publication']['run_path'])
    stdout = next(run.glob('deliveries/*/*/stdout.jsonl'))
    stdout.write_bytes(stdout.read_bytes() + b'{}\n')
    with pytest.raises(ValueError):
        load_catalog(case['root'], case['assembly'].run_id,
                     case['publication']['manifest_id'], case['rules'])


def test_synthetic_three_stage_dispatch_persists_final_uid(output_case, monkeypatch):
    from app.analysis_packets.advanced import build_reconcile, build_synthesis
    from app.analysis_packets.publication import publish_assembly
    from app.analysis_results.acceptance import bind_output_schema, load_catalog
    case = output_case
    request, config = setup_request(case)

    def run_stage():
        last = None
        for packet in case['assembly'].packets:
            req = replace(request, attempt_id=str(uuid4()))
            install_boundary(monkeypatch, case, req, config, packet=packet)
            last = dispatch(case, req, config, packet, authorized=True)
            assert last['status'] == 'accepted'
        return last

    def catalog():
        return load_catalog(case['root'], case['assembly'].run_id,
                            case['publication']['manifest_id'], case['rules'])

    def publish(assembly, accepted):
        assembly.previous_manifest_sha256 = case['publication']['manifest_sha256']
        case['assembly'] = register_members(bind_output_schema(assembly))
        case['publication'] = publish_assembly(case['root'], case['assembly'],
                                               case['bundle'], accepted=accepted)

    run_stage()
    accepted = catalog()
    publish(build_reconcile(case['bundle'], case['assembly'], accepted, case['budget']), accepted)
    run_stage()
    accepted = catalog()
    uid = next(iter(case['bundle'].users))
    publish(build_synthesis(case['bundle'], uid, 0, case['assembly'], accepted, case['budget']),
            accepted)
    last = run_stage()
    assert last['profile']['artifact_type'] == 'user_profile'
    path = Path(case['publication']['run_path']) / f'users/{uid}.json'
    assert path.is_file()
    profile = json.loads(path.read_bytes())
    assert profile['analysis_coverage']['status'] == 'complete'
    assert any('子成员实际模型' in item for item in profile['limitations'])


def test_response_cannot_diverge_from_native_proof(output_case, monkeypatch):
    from app.analysis_execution import dispatch as module
    case = output_case
    request, config = setup_request(case)
    install_boundary(monkeypatch, case, request, config)
    original = module.accept_response

    def switched_response(root, run_id, manifest_id, task_id, raw, **kwargs):
        value = json.loads(raw)
        value['summary'] = 'changed after native completion'
        return original(root, run_id, manifest_id, task_id, json_bytes(value), **kwargs)

    monkeypatch.setattr(module, 'accept_response', switched_response)
    outcome = dispatch(case, request, config, authorized=True)
    assert outcome['status'] == 'rejected'
    assert outcome['receipt']['reason_codes'] == ['delivery_result_mismatch']


def test_waiting_preparation_cannot_become_model_authorization(output_case):
    case = output_case
    request, config = setup_request(case)
    video = Path(case['publication']['run_path']).parent.parent
    prepared = video / 'runs' / case['bundle'].prepared_run_id / 'run.json'
    record = json.loads(prepared.read_bytes())
    record['status'] = 'waiting_policy'
    prepared.write_bytes(json_bytes(record))
    with pytest.raises(ValueError, match='source_not_ready'):
        dispatch(case, request, config, authorized=True)
    assert not (video / 'sessions' / 'cli-calls').exists()


def test_execution_does_not_claim_child_model_verified(output_case, monkeypatch):
    case = output_case
    request, config = setup_request(case)
    install_boundary(monkeypatch, case, request, config)
    assert dispatch(case, request, config, authorized=True)['status'] == 'accepted'
    execution = json.loads((Path(case['publication']['run_path']) / 'execution.json').read_bytes())
    assert execution['reported_model_scope'] == 'main_session_only'
    assert execution['member_models_verified'] is False


def test_partial_snapshot_needs_frozen_opt_in(frozen_case, request, tmp_path, monkeypatch):
    from app.analysis_input.storage import prepare_input
    from app.analysis_packets.builder import build_primary, prepare_resources
    from app.analysis_packets.publication import publish_assembly
    from app.analysis_packets.source import load_source
    from app.analysis_results.acceptance import bind_output_schema

    frozen_case[1]['coverage'].update(status='partial', main_pagination='partial',
                                      reasons=['main_incomplete'])
    case = request.getfixturevalue('output_case')
    req, config = setup_request(case)
    # Even changing the status label cannot bypass the hashed request's false opt-in.
    video = Path(case['publication']['run_path']).parent.parent
    prepared_path = video / 'runs' / case['bundle'].prepared_run_id / 'run.json'
    record = json.loads(prepared_path.read_bytes())
    assert record['status'] == 'waiting_policy'
    record['status'] = 'ready'
    prepared_path.write_bytes(json_bytes(record))
    with pytest.raises(ValueError, match='source_not_ready'):
        dispatch(case, req, config, authorized=True)
    assert not (video / 'sessions' / 'cli-calls').exists()

    ready = prepare_input(tmp_path / 'source', case['root'], allow_partial=True,
                          context_files={n: tmp_path / n for n in ('rules.md','role.md','coord.md')})
    bundle = load_source(case['root'], ready['analysis_run_id'], case['bundle'].video_id)
    resources, files = prepare_resources(bundle, {
        key: {'name': name, 'version': 'test'} for key, name in
        [('analysis_rules','rules.md'), ('role_prompt','role.md'), ('coordination','coord.md')]})
    assembly = register_members(bind_output_schema(build_primary(
        bundle, str(uuid4()), resources, case['budget'], resource_files=files)))
    case.update(bundle=bundle, assembly=assembly,
                publication=publish_assembly(case['root'], assembly, bundle))
    req, config = setup_request(case)
    calls = install_boundary(monkeypatch, case, req, config)
    assert dispatch(case, req, config, authorized=True)['status'] == 'accepted'
    assert len(calls) == 1


def test_label_catalog_cannot_change_inside_existing_run(output_case, monkeypatch):
    case = output_case
    req, config = setup_request(case)
    calls = install_boundary(monkeypatch, case, req, config)
    assert dispatch(case, req, config, authorized=True)['status'] == 'accepted'
    dimension = next(iter(case['rules']['labels']))
    case['rules']['labels'][dimension].append('new runtime label')
    with pytest.raises(ValueError):
        dispatch(case, replace(req, attempt_id=str(uuid4())), config, authorized=True)
    assert len(calls) == 1


def test_catalog_reload_rejects_different_runtime_labels(output_case, monkeypatch):
    from copy import deepcopy

    from app.analysis_results.acceptance import load_catalog
    case = output_case
    req, config = setup_request(case)
    install_boundary(monkeypatch, case, req, config)
    assert dispatch(case, req, config, authorized=True)['status'] == 'accepted'
    changed = deepcopy(case['rules'])
    changed['labels'][next(iter(changed['labels']))].append('different catalogue')
    with pytest.raises(ValueError, match='rule_catalog_mismatch'):
        load_catalog(case['root'], case['assembly'].run_id,
                     case['publication']['manifest_id'], changed)


def test_acceptance_rejects_catalog_changed_after_delivery(output_case, monkeypatch):
    from copy import deepcopy

    from app.analysis_execution import dispatch as module
    case = output_case
    req, config = setup_request(case)
    install_boundary(monkeypatch, case, req, config)
    original = module.accept_response

    def switched_catalog(*args, **kwargs):
        changed = deepcopy(kwargs['rule_catalog'])
        changed['labels'][next(iter(changed['labels']))].append('different catalogue')
        return original(*args, **(kwargs | {'rule_catalog': changed}))

    monkeypatch.setattr(module, 'accept_response', switched_catalog)
    outcome = dispatch(case, req, config, authorized=True)
    assert outcome['status'] == 'rejected'
    assert outcome['receipt']['reason_codes'] == ['rule_catalog_mismatch']
