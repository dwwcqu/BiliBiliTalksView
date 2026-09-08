"""Dispatch a frozen task directly to its ordinary worker session."""

from dataclasses import replace
from pathlib import Path

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import require_ignored, safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_results.acceptance import _write_once, accept_response, load_catalog, load_context
from app.analysis_results.types import ExecutionContext

from .delivery import prepare_delivery
from .dispatch import _configuration, _profile
from .pool_binding import ADAPTER, pool_reference, require_ready
from .pool_proof import pool_result
from .proof import FILES


def dispatch_pool_task(root, run_id, manifest_id, task_id, *, request, agent_id,
                       rule_catalog, execution_config, environment, spend_limit,
                       authorized=False):
    from .session_pool import load_session_pool, run_pool_turn

    if authorized is not True:
        raise ValueError('execution_not_authorized')
    if request.agents is not None or request.create_agent:
        raise ValueError('initialization_not_allowed_in_dispatch')
    run, bundle, _, manifest = load_context(root, run_id, manifest_id)
    require_ignored(run)
    with preparation_lock(safe_child(run, '.dispatch.lock')):
        require_ready(run, bundle)
        delivery = prepare_delivery(root, run_id, manifest_id, task_id, request.attempt_id,
                                    agent_id, rule_catalog)
        pool = load_session_pool(run.parent.parent)
        matching = [m for m in pool['members'] if m['agent_id'] == agent_id]
        if (len(matching) != 1 or request.session_id != agent_id
                or request.cwd.resolve() != run.parent.parent.resolve()
                or delivery.session_id != pool['main']['agent_id']
                or pool['video_id'] != bundle.video_id or pool['model'] != request.model
                or any(matching[0][k] != delivery.member[k]
                       for k in ('member_id', 'role'))):
            raise ValueError('pool_registry_conflict')
        config = _configuration(delivery, manifest, execution_config, request, rule_catalog)
        if config['provider'] != pool['provider']:
            raise ValueError('execution_config_mismatch')
        config.update(adapter=ADAPTER, pool_ref=pool_reference(run.parent.parent),
                      reported_model_scope='worker_session', member_models_verified=True)
        raw = json_bytes(config)
        ref = {'path': 'execution.json', 'sha256': sha(raw)}
        _write_once(safe_child(run, 'execution.json'), raw)
        catalog = load_catalog(root, run_id, manifest_id, rule_catalog)
        if task_id in catalog:
            receipt = loads(safe_child(run, f'validation/{task_id}/accepted.json').read_text(
                encoding='utf-8'))
            return {'status': 'accepted', 'receipt': receipt, 'reused': True,
                    'profile': _profile(root, run_id, manifest_id, delivery.packet, rule_catalog, ref)}
        request = replace(request, prompt=delivery.message, direct_session=True,
                          bare=True, agents=None, create_agent=False)
        result = run_pool_turn(run.parent.parent, matching[0]['member_id'], request=request,
                               environment=environment, expected_model=config['reported_model'],
                               spend_limit=spend_limit, authorized=True)
        if result['status'] != 'succeeded':
            return result | {'profile': None}
        try:
            response = pool_result(result['result'], delivery.delivery_sha256)
        except ValueError as exc:
            return {'status': 'blocked', 'error_code': str(exc), 'profile': None,
                    'transport': result['transport']}
        records = Path(result['records_path'])
        data = {'delivery': delivery.message.encode('utf-8'),
                'prompt': delivery.message.encode('utf-8'),
                'stdout': safe_child(records, 'stdout.jsonl').read_bytes(),
                'request': safe_child(records, 'request.json').read_bytes(),
                'outcome': safe_child(records, 'outcome.json').read_bytes()}
        prefix = f'deliveries/{task_id}/{request.attempt_id}'
        files = {}
        for name, filename in FILES.items():
            path = prefix + '/' + filename
            _write_once(safe_child(run, path), data[name])
            files[name] = {'path': path, 'sha256': sha(data[name])}
        proof = {'adapter': ADAPTER, 'run_id': run_id, 'task_id': task_id,
                 'attempt_id': request.attempt_id, 'input_sha256': delivery.input_sha256,
                 'session_id': delivery.session_id, 'agent_id': agent_id,
                 'worker_session_id': agent_id, 'pool_ref': config['pool_ref'],
                 'delivery_sha256': delivery.delivery_sha256, 'files': files}
        raw = json_bytes(proof)
        path = prefix + '/delivery-proof.json'
        _write_once(safe_child(run, path), raw)
        context = ExecutionContext(request.attempt_id, delivery.input_sha256, True, True,
                                   ref, agent_id, {'path': path, 'sha256': sha(raw)})
        receipt = accept_response(root, run_id, manifest_id, task_id, json_bytes(response),
                                  execution=context, rule_catalog=rule_catalog)
        profile = (_profile(root, run_id, manifest_id, delivery.packet, rule_catalog, ref)
                   if receipt['decision'] == 'accepted' else None)
        return {'status': receipt['decision'], 'receipt': receipt, 'profile': profile,
                'transport': result['transport'], 'reused': False}
