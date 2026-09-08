"""Recheck the frozen native delivery evidence used by an accepted response."""

from pathlib import Path

from app.analysis_input.storage import safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads

from .evidence import verify_delivery

FILES = {
    'delivery': 'delivery.json', 'prompt': 'prompt.txt', 'stdout': 'stdout.jsonl',
    'request': 'cli-request.json', 'outcome': 'cli-outcome.json',
}


def verify_proof(run: Path, ref: dict, *, packet: dict, execution, config: dict) -> str:
    from .pool_binding import ADAPTER

    if config.get('adapter') == ADAPTER:
        from .pool_proof import verify_pool_proof
        return verify_pool_proof(run, ref, packet=packet, execution=execution, config=config)
    if config.get('adapter') not in (None, 'claude-code-sendmessage-2.1.261-v1'):
        raise ValueError('unsupported_evidence_adapter')
    if (config.get('reported_model_scope') != 'main_session_only'
            or config.get('member_models_verified') is not False):
        raise ValueError('invalid_model_provenance_scope')
    prefix = f"deliveries/{packet['task_id']}/{execution.attempt_id}"
    if ref.get('path') != prefix + '/delivery-proof.json':
        raise ValueError('invalid_delivery_proof_path')
    raw = safe_child(run, ref['path']).read_bytes()
    if sha(raw) != ref.get('sha256'):
        raise ValueError('delivery_proof_changed')
    proof = loads(raw.decode('utf-8'))
    expected = {
        'adapter': 'claude-code-sendmessage-2.1.261-v1',
        'run_id': packet['run_id'], 'task_id': packet['task_id'],
        'attempt_id': execution.attempt_id, 'input_sha256': execution.input_sha256,
        'session_id': config['session_id'], 'agent_id': execution.agent_id,
    }
    if any(proof.get(k) != v for k, v in expected.items()) or set(proof['files']) != set(FILES):
        raise ValueError('delivery_proof_identity_mismatch')
    data = {}
    for name, filename in FILES.items():
        file_ref = proof['files'][name]
        if file_ref['path'] != prefix + '/' + filename:
            raise ValueError('invalid_delivery_proof_path')
        data[name] = safe_child(run, file_ref['path']).read_bytes()
        if sha(data[name]) != file_ref['sha256']:
            raise ValueError('delivery_proof_changed')
    wrapper = loads(data['delivery'].decode('utf-8'))
    digest = sha(json_bytes(wrapper['payload']))
    if (wrapper['payload_sha256'] != digest or proof['delivery_sha256'] != digest
            or json_bytes(wrapper['payload']['packet']) != json_bytes(packet)):
        raise ValueError('delivery_proof_input_mismatch')
    if (sha(json_bytes(wrapper['payload']['label_catalog']))
            != config.get('label_catalog_sha256')):
        raise ValueError('delivery_label_catalog_mismatch')
    request = loads(data['request'].decode('utf-8'))
    outcome = loads(data['outcome'].decode('utf-8'))
    if (request['session_id'] != config['session_id']
            or request['attempt_id'] != execution.attempt_id
            or request['prompt_sha256'] != sha(data['prompt'])
            or request['configured_model'] != config['configured_model']
            or request['resume'] is not True
            or Path(request['cwd']).resolve() != run.parent.parent.resolve()
            or outcome['session_id'] != config['session_id']
            or outcome['attempt_id'] != execution.attempt_id
            or outcome['status'] != 'transport_succeeded'
            or type(outcome['returncode']) is not int or outcome['returncode'] != 0
            or outcome['stdout_sha256'] != sha(data['stdout'])):
        raise ValueError('delivery_proof_transport_mismatch')
    evidence = verify_delivery(
        data['stdout'], session_id=config['session_id'], agent_id=execution.agent_id,
        message=data['delivery'].decode('utf-8'), delivery_sha256=digest,
        expected_model=config['reported_model'], cli_version=config['cli_version'],
        cwd=request['cwd'],
    )
    if evidence['tool_use_id'] != proof['tool_use_id']:
        raise ValueError('delivery_proof_tool_mismatch')

    return sha(json_bytes(evidence['task_result']))
