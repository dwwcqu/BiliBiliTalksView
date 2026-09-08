"""Ordinary session delivery still requires the full frozen output contract."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from result_examples import response_for
from test_dispatch import setup_request

from app.analysis_packets.codec import json_bytes, loads


def pool_case(case):
    from app.analysis_execution.pool_binding import bind_session_pool, prepare_session_pool
    from app.analysis_results.acceptance import read_state
    req, config = setup_request(case)
    pool = prepare_session_pool(
        case['root'], case['assembly'].run_id, case['publication']['manifest_id'],
        rule_catalog=case['rules'], model=req.model, provider=config['provider'],
        members=[{'member_id': 'worker-' + role.replace('_', '-'), 'role': role}
                 for role in ('user_initial', 'thread_context', 'user_synthesis')])
    original = Path(case['publication']['manifest_path']).read_bytes()
    old_path = Path(case['publication']['manifest_path'])
    bound = bind_session_pool(case['root'], case['assembly'].run_id,
                              case['publication']['manifest_id'], case['rules'])
    assert old_path.read_bytes() == original
    case['publication'] = bound
    _, _, case['assembly'], _ = read_state(case['root'], case['assembly'].run_id,
                                          bound['manifest_id'], case['rules'])
    req, config = setup_request(case)
    return pool, req, config


def invoke(case, req, config, packet=None):
    from app.analysis_execution.pool_dispatch import dispatch_pool_task
    packet = packet or case['assembly'].packets[0]
    member = next(m for m in case['assembly'].member_registry['members']
                  if packet['task_id'] in m['task_ids'])
    return dispatch_pool_task(
        case['root'], case['assembly'].run_id, case['publication']['manifest_id'],
        packet['task_id'], request=replace(req, session_id=member['agent_id']),
        agent_id=member['agent_id'], rule_catalog=case['rules'], execution_config=config,
        environment={}, spend_limit=Decimal(1), authorized=True)


def boundary(monkeypatch, req, config, *, wrong_digest=False):
    from app.analysis_execution import runner
    calls = []

    def run(argv, **kwargs):
        wrapper = loads(kwargs['stdin'].decode())
        payload = wrapper['payload']
        session = argv[argv.index('--resume' if '--resume' in argv else '--session-id') + 1]
        assert argv[argv.index('--tools') + 1] == 'Read'
        assert '--agents' not in argv
        result = {'delivery_sha256': '0' * 64 if wrong_digest else wrapper['payload_sha256'],
                  'task_result': response_for(payload['packet'], payload['attempt_id'])}
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': session,
             'model': config['reported_model'], 'claude_code_version': '2.1.261',
             'cwd': str(req.cwd), 'tools': ['Read']},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': session, 'result': json_bytes(result).decode(),
             'total_cost_usd': Decimal('0.01')}]
        calls.append(argv)
        return {'stdout': b''.join(json_bytes(e) for e in events), 'stderr': b'',
                'returncode': 0, 'stop_reason': None}
    monkeypatch.setattr(runner, 'run_process', run)
    return calls


def test_pool_binding_and_direct_delivery_are_reusable(output_case, monkeypatch):
    from app.analysis_results.acceptance import load_catalog
    pool, req, config = pool_case(output_case)
    calls = boundary(monkeypatch, req, config)
    result = invoke(output_case, req, config)
    assert result['status'] == 'accepted'
    assert '--session-id' in calls[0]
    assert invoke(output_case, req, config)['reused'] is True
    assert len(calls) == 1
    assert load_catalog(output_case['root'], output_case['assembly'].run_id,
                        output_case['publication']['manifest_id'], output_case['rules'])
    run = Path(output_case['publication']['run_path'])
    execution = loads((run / 'execution.json').read_text())
    assert execution['session_id'] == pool['main']['agent_id']
    assert execution['adapter'] == 'claude-code-session-2.1.261-v1'
    assert execution['reported_model_scope'] == 'worker_session'


def test_pool_wrong_digest_never_accepts(output_case, monkeypatch):
    _, req, config = pool_case(output_case)
    boundary(monkeypatch, req, config, wrong_digest=True)
    result = invoke(output_case, req, config)
    assert result['status'] == 'blocked'
    assert result['error_code'] == 'delivery_digest_mismatch'
    assert not (Path(output_case['publication']['run_path']) / 'validation').exists()


def test_pool_waiting_source_never_calls(output_case, monkeypatch):
    _, req, config = pool_case(output_case)
    calls = boundary(monkeypatch, req, config)
    path = req.cwd / 'runs' / output_case['bundle'].prepared_run_id / 'run.json'
    record = loads(path.read_text())
    record['status'] = 'waiting_policy'
    path.write_bytes(json_bytes(record))
    with pytest.raises(ValueError, match='source_not_ready'):
        invoke(output_case, req, config)
    assert calls == []


def test_pool_proof_rejects_tampered_output(output_case, monkeypatch):
    from app.analysis_results.acceptance import load_catalog
    _, req, config = pool_case(output_case)
    boundary(monkeypatch, req, config)
    assert invoke(output_case, req, config)['status'] == 'accepted'
    run = Path(output_case['publication']['run_path'])
    task = output_case['assembly'].packets[0]['task_id']
    (run / 'deliveries' / task / req.attempt_id / 'stdout.jsonl').write_bytes(b'{}')
    with pytest.raises(ValueError):
        load_catalog(output_case['root'], output_case['assembly'].run_id,
                     output_case['publication']['manifest_id'], output_case['rules'])


def test_pool_all_stages_save_uid_file(output_case, monkeypatch):
    from app.analysis_execution.pool_binding import bind_session_pool
    from app.analysis_packets.advanced import build_reconcile, build_synthesis
    from app.analysis_packets.publication import publish_assembly
    from app.analysis_results.acceptance import bind_output_schema, load_catalog, read_state
    case = output_case
    _, req, config = pool_case(case)
    calls = boundary(monkeypatch, req, config)

    def execute_stage():
        last = None
        for packet in case['assembly'].packets:
            last = invoke(case, replace(req, attempt_id=str(uuid4())), config, packet)
            assert last['status'] == 'accepted'
        return last

    def catalog():
        return load_catalog(case['root'], case['assembly'].run_id,
                            case['publication']['manifest_id'], case['rules'])

    def publish(assembly, accepted):
        assembly = bind_output_schema(assembly)
        assembly.previous_manifest_sha256 = case['publication']['manifest_sha256']
        result = publish_assembly(case['root'], assembly, case['bundle'], accepted=accepted)
        bound = bind_session_pool(case['root'], assembly.run_id, result['manifest_id'], case['rules'])
        case['publication'] = bound
        _, _, case['assembly'], _ = read_state(case['root'], assembly.run_id,
                                              bound['manifest_id'], case['rules'])

    execute_stage()
    accepted = catalog()
    publish(build_reconcile(case['bundle'], case['assembly'], accepted, case['budget']), accepted)
    execute_stage()
    accepted = catalog()
    uid = next(iter(case['bundle'].users))
    publish(build_synthesis(case['bundle'], uid, 0, case['assembly'], accepted, case['budget']), accepted)
    final = execute_stage()
    assert final['profile']['artifact_type'] == 'user_profile'
    assert (Path(case['publication']['run_path']) / f'users/{uid}.json').is_file()
    assert any('--resume' in argv for argv in calls)


def test_migration_rejects_old_execution_before_binding(output_case):
    from app.analysis_execution.pool_binding import bind_session_pool, prepare_session_pool
    case = output_case
    prepare_session_pool(case['root'], case['assembly'].run_id,
                         case['publication']['manifest_id'], rule_catalog=case['rules'],
                         model='test', provider='test',
                         members=[{'member_id': 'reader', 'role': 'user_initial'},
                                  {'member_id': 'context', 'role': 'thread_context'}])
    path = Path(case['publication']['run_path']) / 'execution.json'
    path.write_bytes(b'{"adapter":"native"}')
    with pytest.raises(ValueError, match='pool_migration_requires_new_run'):
        bind_session_pool(case['root'], case['assembly'].run_id,
                          case['publication']['manifest_id'], case['rules'])
    assert path.read_bytes() == b'{"adapter":"native"}'


def test_migration_rejects_different_old_members(output_case):
    from app.analysis_execution.pool_binding import bind_session_pool
    from app.analysis_execution.session_pool import create_session_pool
    case = output_case
    registry = case['assembly'].member_registry
    video = Path(case['publication']['run_path']).parent.parent
    create_session_pool(video, video_id=case['bundle'].video_id, model='test', provider='test',
                        members=[{'member_id': 'reader', 'role': 'user_initial',
                                  'role_sha256': case['assembly'].resources['role_prompt']['sha256']}],
                        legacy={'session_id': registry['main']['session_id'], 'members': []})
    with pytest.raises(ValueError, match='pool_migration_source_mismatch'):
        bind_session_pool(case['root'], case['assembly'].run_id,
                          case['publication']['manifest_id'], case['rules'])

def test_prepare_pool_again_from_bound_manifest_preserves_ids(output_case):
    from app.analysis_execution.pool_binding import prepare_session_pool
    pool, req, config = pool_case(output_case)
    repeated = prepare_session_pool(
        output_case['root'], output_case['assembly'].run_id,
        output_case['publication']['manifest_id'], rule_catalog=output_case['rules'],
        model=req.model, provider=config['provider'],
        members=[{'member_id': m['member_id'], 'role': m['role']} for m in pool['members']])
    assert repeated == pool

def test_new_role_file_refreshes_input_without_replacing_pool_sessions(output_case, monkeypatch):
    from app.analysis_execution.pool_binding import bind_session_pool, prepare_session_pool
    from app.analysis_input.storage import prepare_input
    from app.analysis_packets.builder import build_primary, prepare_resources
    from app.analysis_packets.publication import publish_assembly
    from app.analysis_packets.source import load_source
    from app.analysis_results.acceptance import bind_output_schema, read_state
    case = output_case
    pool, req, config = pool_case(case)
    workspace = case['root'].parent
    (workspace / 'role.md').write_text('更新的合成分析角色', encoding='utf-8')
    prepared = prepare_input(workspace / 'source', case['root'],
                             context_files={n: workspace / n
                                            for n in ('rules.md', 'role.md', 'coord.md')})
    bundle = load_source(case['root'], prepared['analysis_run_id'], case['bundle'].video_id)
    resources, files = prepare_resources(bundle, {
        key: {'name': name, 'version': 'updated'} for key, name in
        [('analysis_rules', 'rules.md'), ('role_prompt', 'role.md'), ('coordination', 'coord.md')]})
    assembly = bind_output_schema(build_primary(bundle, str(uuid4()), resources, case['budget'],
                                                resource_files=files))
    publication = publish_assembly(case['root'], assembly, bundle)
    repeated = prepare_session_pool(
        case['root'], assembly.run_id, publication['manifest_id'], rule_catalog=case['rules'],
        model=req.model, provider=config['provider'],
        members=[{'member_id': m['member_id'], 'role': m['role']} for m in pool['members']])
    assert repeated == pool
    bound = bind_session_pool(case['root'], assembly.run_id, publication['manifest_id'], case['rules'])
    _, _, current, _ = read_state(case['root'], assembly.run_id, bound['manifest_id'], case['rules'])
    assert {m['agent_id'] for m in current.member_registry['members']} == {
        m['agent_id'] for m in pool['members']}
    assert all(m['role_sha256'] == resources['role_prompt']['sha256']
               for m in current.member_registry['members'])
    case.update(bundle=bundle, assembly=current, publication=bound)
    updated_request, updated_config = setup_request(case)
    boundary(monkeypatch, updated_request, updated_config)
    assert invoke(case, updated_request, updated_config)['status'] == 'accepted'
