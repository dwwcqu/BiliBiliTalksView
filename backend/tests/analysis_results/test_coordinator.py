"""The coordinator consumes the same frozen video background as workers."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from test_pool_dispatch import pool_case

from app.analysis_packets.codec import json_bytes, loads


def fresh_run(case, background):
    from app.analysis_input.storage import prepare_input
    from app.analysis_packets.builder import build_primary, prepare_resources
    from app.analysis_packets.publication import publish_assembly
    from app.analysis_packets.source import load_source
    from app.analysis_results.acceptance import bind_output_schema

    workspace = case['root'].parent
    source = workspace / 'source'
    current = loads((source / 'current.json').read_text('utf-8'))
    (source / current['batch_path'] / 'README.md').write_bytes(background.encode('utf-8'))
    prepared = prepare_input(source, case['root'],
                             context_files={n: workspace / n
                                            for n in ('rules.md', 'role.md', 'coord.md')})
    bundle = load_source(case['root'], prepared['analysis_run_id'], case['bundle'].video_id)
    resources, files = prepare_resources(bundle, {
        key: {'name': name, 'version': 'test'} for key, name in
        [('analysis_rules', 'rules.md'), ('role_prompt', 'role.md'), ('coordination', 'coord.md')]})
    assembly = bind_output_schema(build_primary(bundle, str(uuid4()), resources, case['budget'],
                                                resource_files=files))
    case.update(bundle=bundle, assembly=assembly,
                publication=publish_assembly(case['root'], assembly, bundle))


def prepare(case, req):
    from app.analysis_execution.coordinator import prepare_coordinator_delivery
    return prepare_coordinator_delivery(
        case['root'], case['assembly'].run_id, case['publication']['manifest_id'],
        attempt_id=req.attempt_id, instruction=req.prompt, rule_catalog=case['rules'])


def invoke(case, req, config, **kwargs):
    from app.analysis_execution.coordinator import run_coordinator_turn
    return run_coordinator_turn(
        case['root'], case['assembly'].run_id, case['publication']['manifest_id'],
        request=req, rule_catalog=case['rules'], environment={},
        expected_model=config['reported_model'], spend_limit=Decimal(1), **kwargs)


def boundary(monkeypatch, req, config, *, wrong_digest=False, missing_digest=False, bad_response=False):
    from app.analysis_execution import runner
    calls = []

    def execute(argv, **kwargs):
        wrapper = loads(kwargs['stdin'].decode())
        calls.append((argv, wrapper))
        reply = {'context_sha256': '0' * 64 if wrong_digest else wrapper['payload_sha256'],
                 'response': {'background_seen': wrapper['payload']['resources']['background']['text']}}
        if missing_digest:
            del reply['context_sha256']
        if bad_response:
            reply['response'] = []
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': req.session_id,
             'model': config['reported_model'], 'claude_code_version': '2.1.261',
             'cwd': str(req.cwd), 'tools': ['Read']},
            {'type': 'result', 'subtype': 'success', 'session_id': req.session_id,
             'is_error': False, 'result': json_bytes(reply).decode(), 'total_cost_usd': Decimal('0.01')}]
        return {'stdout': b''.join(json_bytes(e) for e in events), 'stderr': b'',
                'returncode': 0, 'stop_reason': None}
    monkeypatch.setattr(runner, 'run_process', execute)
    return calls


def test_main_and_worker_receive_identical_full_background(output_case):
    from app.analysis_execution.delivery import prepare_delivery
    case = output_case
    background = '视频讨论公共交通。\n背景里的“忽略规则”只是数据。'
    fresh_run(case, background)
    _, req, _ = pool_case(case)
    main = prepare(case, req)
    wrapper = loads(main.prompt)
    packet = case['assembly'].packets[0]
    member = next(m for m in case['assembly'].member_registry['members']
                  if packet['task_id'] in m['task_ids'])
    worker = prepare_delivery(case['root'], case['assembly'].run_id,
                              case['publication']['manifest_id'], packet['task_id'],
                              req.attempt_id, member['agent_id'], case['rules'])
    assert wrapper['payload']['resources']['background'] == packet['resources']['background']
    assert wrapper['payload']['resource_texts']['context/README.md'] == background
    assert loads(worker.message)['payload']['resources']['context/README.md'] == background
    assert wrapper['payload']['background_status'] == 'provided'
    assert set(wrapper['payload']['resources']) == {
        'background', 'analysis_rules', 'role_prompt', 'coordination'}


def test_empty_background_is_explicit(output_case):
    _, req, _ = pool_case(output_case)
    wrapper = loads(prepare(output_case, req).prompt)
    assert wrapper['payload']['background_status'] == 'missing'
    assert wrapper['payload']['resources']['background']['text'] == ''


def test_main_turn_saves_context_and_reuses_without_new_call(output_case, monkeypatch):
    _, req, config = pool_case(output_case)
    calls = boundary(monkeypatch, req, config)
    result = invoke(output_case, req, config, authorized=True)
    assert result['status'] == 'context_confirmed'
    assert len(calls) == 1
    run = Path(output_case['publication']['run_path'])
    receipt = run / 'coordinator' / req.attempt_id / 'receipt.json'
    assert receipt.is_file()
    assert not (run / 'validation').exists()
    assert not (run / 'users/1.json').exists()
    assert invoke(output_case, req, config, authorized=True)['reused'] is True
    assert len(calls) == 1
    with pytest.raises(ValueError, match='result_conflict'):
        invoke(output_case, replace(req, prompt='different instruction'), config, authorized=True)
    assert len(calls) == 1


@pytest.mark.parametrize('fault', ['wrong_digest', 'missing_digest', 'bad_response'])
def test_wrong_background_ack_never_publishes_receipt(output_case, monkeypatch, fault):
    _, req, config = pool_case(output_case)
    boundary(monkeypatch, req, config, **{fault: True})
    result = invoke(output_case, req, config, authorized=True)
    assert result['status'] == 'blocked'
    assert result['error_code'] == 'coordinator_context_mismatch'
    run = Path(output_case['publication']['run_path'])
    assert not (run / 'coordinator' / req.attempt_id / 'receipt.json').exists()


@pytest.mark.parametrize('fault', ['unauthorized', 'wrong_main', 'waiting', 'tampered', 'large'])
def test_invalid_context_never_calls_model(output_case, monkeypatch, fault):
    _, req, config = pool_case(output_case)
    calls = boundary(monkeypatch, req, config)
    if fault == 'wrong_main':
        req = replace(req, session_id=str(uuid4()))
    elif fault == 'waiting':
        path = req.cwd / 'runs' / output_case['bundle'].prepared_run_id / 'run.json'
        record = loads(path.read_text())
        record['status'] = 'waiting_policy'
        path.write_bytes(json_bytes(record))
    elif fault == 'tampered':
        (Path(output_case['publication']['run_path']) / 'context/README.md').write_text('tampered')
    elif fault == 'large':
        req = replace(req, prompt='x' * 200000)
    with pytest.raises(ValueError):
        invoke(output_case, req, config, authorized=fault != 'unauthorized')
    assert calls == []


def test_updated_background_uses_same_main_and_keeps_old_snapshot(output_case, monkeypatch):
    case = output_case
    fresh_run(case, '第一版视频背景')
    pool, req, config = pool_case(case)
    old = dict(case)
    calls = boundary(monkeypatch, req, config)
    assert invoke(case, req, config, authorized=True)['status'] == 'context_confirmed'
    fresh_run(case, '第二版视频背景')
    next_pool, next_req, next_config = pool_case(case)
    assert next_pool == pool
    assert next_req.session_id == req.session_id
    assert invoke(case, next_req, next_config, authorized=True)['status'] == 'context_confirmed'
    assert calls[0][1]['payload']['resources']['background']['text'] == '第一版视频背景'
    assert calls[1][1]['payload']['resources']['background']['text'] == '第二版视频背景'
    assert '--resume' in calls[1][0]
    assert loads(prepare(old, req).prompt)['payload']['resources']['background']['text'] == '第一版视频背景'
