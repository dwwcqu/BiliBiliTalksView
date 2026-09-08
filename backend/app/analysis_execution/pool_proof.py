"""Ordinary worker proof, separate from native child evidence."""

from pathlib import Path

from app.analysis_input.storage import safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads

from .pool_binding import ADAPTER, pool_reference
from .session_evidence import verify_session_output


def pool_result(result, digest):
    if not isinstance(result, dict) or result.get('delivery_sha256') != digest:
        raise ValueError('delivery_digest_mismatch')
    if set(result) != {'delivery_sha256', 'task_result'} or not isinstance(result['task_result'], dict):
        raise ValueError('invalid_task_result')
    return result['task_result']


def verify_pool_proof(run, ref, *, packet, execution, config):
    from .proof import FILES
    from .session_pool import load_session_pool

    if (config.get('adapter') != ADAPTER or config.get('reported_model_scope') != 'worker_session'
            or config.get('member_models_verified') is not True):
        raise ValueError('invalid_model_provenance_scope')
    video = run.parent.parent
    if config.get('pool_ref') != pool_reference(video):
        raise ValueError('pool_reference_changed')
    pool = load_session_pool(video)
    members = [m for m in pool['members'] if m['agent_id'] == execution.agent_id]
    if (pool['main']['agent_id'] != config['session_id'] or len(members) != 1
            or members[0]['role'] != packet['task_type']
            or pool['model'] != config['configured_model'] or pool['provider'] != config['provider']):
        raise ValueError('pool_registry_conflict')
    prefix = f'deliveries/{packet["task_id"]}/{execution.attempt_id}'
    if ref.get('path') != prefix + '/delivery-proof.json':
        raise ValueError('invalid_delivery_proof_path')
    raw = safe_child(run, ref['path']).read_bytes()
    if sha(raw) != ref.get('sha256'):
        raise ValueError('delivery_proof_changed')
    proof = loads(raw.decode('utf-8'))
    expected = {'adapter': ADAPTER, 'run_id': packet['run_id'], 'task_id': packet['task_id'],
                'attempt_id': execution.attempt_id, 'input_sha256': execution.input_sha256,
                'session_id': config['session_id'], 'agent_id': execution.agent_id,
                'worker_session_id': execution.agent_id, 'pool_ref': config['pool_ref']}
    if any(proof.get(k) != v for k, v in expected.items()) or set(proof['files']) != set(FILES):
        raise ValueError('delivery_proof_identity_mismatch')
    data = {}
    for name, filename in FILES.items():
        entry = proof['files'][name]
        if entry['path'] != prefix + '/' + filename:
            raise ValueError('invalid_delivery_proof_path')
        data[name] = safe_child(run, entry['path']).read_bytes()
        if sha(data[name]) != entry['sha256']:
            raise ValueError('delivery_proof_changed')
    wrapper = loads(data['delivery'].decode('utf-8'))
    payload = wrapper['payload']
    digest = sha(json_bytes(payload))
    if (wrapper['payload_sha256'] != digest or proof['delivery_sha256'] != digest
            or json_bytes(payload['packet']) != json_bytes(packet)
            or payload['agent_id'] != execution.agent_id
            or payload['attempt_id'] != execution.attempt_id
            or payload['input_sha256'] != execution.input_sha256
            or data['prompt'] != data['delivery']):
        raise ValueError('delivery_proof_input_mismatch')
    if sha(json_bytes(payload['label_catalog'])) != config['label_catalog_sha256']:
        raise ValueError('delivery_label_catalog_mismatch')
    request = loads(data['request'].decode('utf-8'))
    outcome = loads(data['outcome'].decode('utf-8'))
    if (request['session_id'] != execution.agent_id
            or request['attempt_id'] != execution.attempt_id
            or request['prompt_sha256'] != sha(data['prompt'])
            or request['prompt_bytes'] != len(data['prompt'])
            or request['configured_model'] != config['configured_model']
            or type(request['resume']) is not bool or request.get('direct_session') is not True
            or request.get('create_agent') is not False or request.get('agents_sha256') is not None
            or request.get('bare') is not True or request.get('cli_state_dir') != pool['cli_state_dir']
            or Path(request['cwd']).resolve() != video.resolve()
            or outcome['session_id'] != execution.agent_id
            or outcome['attempt_id'] != execution.attempt_id
            or outcome['status'] != 'transport_succeeded'
            or type(outcome['returncode']) is not int or outcome['returncode'] != 0
            or outcome['stdout_sha256'] != sha(data['stdout'])):
        raise ValueError('delivery_proof_transport_mismatch')
    result = verify_session_output(data['stdout'], session_id=execution.agent_id,
                                    expected_model=config['reported_model'],
                                    cli_version=config['cli_version'], cwd=request['cwd'])
    return sha(json_bytes(pool_result(result, digest)))
