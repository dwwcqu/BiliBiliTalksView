"""Execute one frozen task with an existing member and accept native evidence."""

from dataclasses import replace

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import require_ignored, safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_packets.publication import _read
from app.analysis_results.acceptance import (
    _write_once,
    accept_response,
    load_catalog,
    load_context,
)
from app.analysis_results.profiles import save_profile
from app.analysis_results.types import ExecutionContext

from .command import CliRequest, build_command
from .delivery import prepare_delivery
from .evidence import verify_delivery
from .proof import FILES
from .runner import execute


def _configuration(delivery, manifest, supplied, request, rule_catalog):
    registry = loads(_read(delivery.run_path, manifest['member_registry']).decode('utf-8'))
    expected = {
        'run_id': delivery.packet['run_id'],
        'rules_sha256': delivery.packet['resources']['analysis_rules']['sha256'],
        'session_id': delivery.session_id,
        'agent_ids': [m['agent_id'] for m in registry['members']],
        'input_protocol_version': '2.0.0', 'output_protocol_version': '1.0.0',
        'limits': manifest['execution_limits'],
    }
    if not isinstance(supplied, dict) or any(supplied.get(k) != v for k, v in expected.items()):
        raise ValueError('execution_config_mismatch')
    if (supplied.get('cli_version') != '2.1.261'
            or supplied.get('configured_model') != request.model
            or any(not isinstance(supplied.get(k), str) or not supplied[k].strip()
                   for k in ('reported_model', 'provider'))):
        raise ValueError('execution_config_mismatch')
    return (expected | {k: supplied[k] for k in (
        'cli_version', 'configured_model', 'reported_model', 'provider')}
        | {'reported_model_scope': 'main_session_only', 'member_models_verified': False,
           'label_catalog_sha256': sha(json_bytes(rule_catalog))})


def _transport(request, records_root, environment):
    attempt = safe_child(records_root, f'{request.session_id}/{request.attempt_id}')
    if not attempt.exists():
        execute(request, records_root=records_root, environment=environment, authorized=True)
    try:
        request_raw = safe_child(attempt, 'request.json').read_bytes()
        outcome_raw = safe_child(attempt, 'outcome.json').read_bytes()
        saved = loads(request_raw.decode('utf-8'))
        expected = {
            'session_id': request.session_id, 'attempt_id': request.attempt_id,
            'configured_model': request.model, 'resume': True,
            'executable': str(request.executable), 'cwd': str(request.cwd),
            'prompt_sha256': sha(request.prompt.encode('utf-8')),
            'prompt_bytes': len(request.prompt.encode('utf-8')),
            'budget_usd': str(request.budget_usd), 'timeout_seconds': request.timeout_seconds,
            'max_output_bytes': request.max_output_bytes,
            'cli_state_dir': environment.get('CLAUDE_CONFIG_DIR'),
            'create_agent': False, 'bare': False,
            'agents_sha256': sha(json_bytes(request.agents)) if request.agents is not None else None,
        }
        if any(saved.get(k) != v for k, v in expected.items()):
            raise ValueError('transport_request_changed')
        outcome = loads(outcome_raw.decode('utf-8'))
        raw = safe_child(attempt, 'stdout.jsonl').read_bytes()
        if (outcome['stdout_sha256'] != sha(raw)
                or outcome['session_id'] != request.session_id
                or outcome['attempt_id'] != request.attempt_id):
            raise ValueError('transport_record_changed')
        return raw, request_raw, outcome_raw, outcome
    except OSError as exc:
        raise ValueError('session_recovery_required') from exc


def _profile(root, run_id, manifest_id, packet, rules, ref):
    if packet['task_type'] != 'user_synthesis':
        return None
    return save_profile(root, run_id, manifest_id, packet['scope']['target_uid'], rules, ref)


def dispatch_task(root, run_id, manifest_id, task_id, *, request: CliRequest, agent_id,
                  rule_catalog, execution_config, environment, authorized=False) -> dict:
    if authorized is not True:
        raise ValueError('execution_not_authorized')
    if request.agents is not None or request.create_agent:
        raise ValueError('initialization_not_allowed_in_dispatch')
    if (not isinstance(environment, dict)
            or any(not isinstance(k, str) or not k or '=' in k or '\0' in k
                   or not isinstance(v, str) or '\0' in v for k, v in environment.items())):
        raise ValueError('invalid_execution_environment')
    run, _, _, _ = load_context(root, run_id, manifest_id)
    require_ignored(run)
    with preparation_lock(safe_child(run, '.dispatch.lock')):
        delivery = prepare_delivery(root, run_id, manifest_id, task_id, request.attempt_id,
                                    agent_id, rule_catalog)
        if (request.resume is not True or request.session_id != delivery.session_id
                or request.cwd.resolve() != run.parent.parent.resolve()):
            raise ValueError('registered_session_required')
        from .team_binding import team_definitions, team_environment

        environment = team_environment(run.parent.parent, delivery.session_id,
                                       [delivery.member], environment)
        definitions = team_definitions(run.parent.parent, delivery.member['member_id'])
        request = replace(request, prompt=delivery.prompt, bare=False,
                          agents=definitions, create_agent=False)
        build_command(request)
        _, bundle, _, manifest = load_context(root, run_id, manifest_id)
        prepared = loads(safe_child(
            run.parent.parent, f'runs/{bundle.prepared_run_id}/run.json'
        ).read_text(encoding='utf-8'))
        allow_partial = prepared['request']['allow_partial']
        source = bundle.manifest
        if (type(allow_partial) is not bool or prepared['status'] != 'ready'
                or source['counts']['known_users'] == 0
                or (source['coverage']['status'] == 'partial' and not allow_partial)):
            raise ValueError('source_not_ready')
        config = _configuration(delivery, manifest, execution_config, request, rule_catalog)
        config_raw = json_bytes(config)
        ref = {'path': 'execution.json', 'sha256': sha(config_raw)}
        _write_once(safe_child(run, 'execution.json'), config_raw)
        catalog = load_catalog(root, run_id, manifest_id, rule_catalog)
        if task_id in catalog:
            receipt = loads(safe_child(run, f'validation/{task_id}/accepted.json').read_text(
                encoding='utf-8'))
            return {'status': 'accepted', 'receipt': receipt, 'reused': True,
                    'profile': _profile(root, run_id, manifest_id, delivery.packet,
                                        rule_catalog, ref)}
        prefix = f'deliveries/{task_id}/{request.attempt_id}'
        _write_once(safe_child(run, prefix + '/delivery.json'), delivery.message.encode('utf-8'))
        _write_once(safe_child(run, prefix + '/prompt.txt'), request.prompt.encode('utf-8'))
        records_root = safe_child(run.parent.parent, 'sessions/cli-calls')
        stdout, request_raw, outcome_raw, transport = _transport(request, records_root, environment)
        if transport['status'] != 'transport_succeeded' or transport['returncode'] != 0:
            return {'status': 'blocked', 'error_code': transport.get('error_code'),
                    'transport': transport, 'profile': None}
        try:
            native = verify_delivery(
                stdout, session_id=delivery.session_id, agent_id=agent_id,
                message=delivery.message, delivery_sha256=delivery.delivery_sha256,
                expected_model=config['reported_model'], cli_version=config['cli_version'],
                cwd=request.cwd,
            )
        except ValueError as exc:
            return {'status': 'blocked', 'error_code': str(exc),
                    'transport': transport, 'profile': None}
        data = {'delivery': delivery.message.encode('utf-8'),
                'prompt': request.prompt.encode('utf-8'), 'stdout': stdout,
                'request': request_raw, 'outcome': outcome_raw}
        file_refs = {}
        for name, filename in FILES.items():
            path = prefix + '/' + filename
            _write_once(safe_child(run, path), data[name])
            file_refs[name] = {'path': path, 'sha256': sha(data[name])}
        proof = {
            'adapter': 'claude-code-sendmessage-2.1.261-v1', 'run_id': run_id,
            'task_id': task_id, 'attempt_id': request.attempt_id,
            'input_sha256': delivery.input_sha256, 'session_id': delivery.session_id,
            'agent_id': agent_id, 'delivery_sha256': delivery.delivery_sha256,
            'tool_use_id': native['tool_use_id'], 'files': file_refs,
        }
        proof_raw = json_bytes(proof)
        path = prefix + '/delivery-proof.json'
        _write_once(safe_child(run, path), proof_raw)
        context = ExecutionContext(request.attempt_id, delivery.input_sha256,
                                   True, True, ref, agent_id,
                                   {'path': path, 'sha256': sha(proof_raw)})
        receipt = accept_response(root, run_id, manifest_id, task_id,
                                  json_bytes(native['task_result']), execution=context,
                                  rule_catalog=rule_catalog)
        profile = (_profile(root, run_id, manifest_id, delivery.packet, rule_catalog, ref)
                   if receipt['decision'] == 'accepted' else None)
        return {'status': receipt['decision'], 'receipt': receipt, 'profile': profile,
                'transport': transport, 'reused': False}
