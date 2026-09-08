"""Explicit same-child handshake recovery, with immutable native and spending evidence."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import require_ignored, safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads

from . import initialization as init
from .command import CliRequest, build_command
from .creation_evidence import _safe_id
from .events import equivalent_message, parse_events
from .evidence import _blocks, _json_object, _require, verify_delivery


def inspect_pending_birth(intent, step, data):
    """Return a management handle only; this never verifies a handshake."""
    budget_stop = init._transport(intent, step, data, allow_budget_stop=True)
    events = [loads(line) for line in data['stdout'].decode().splitlines() if line.strip()]
    initialized = terminal = trailing = saw_stop = False
    tool = agent = correction = None
    started = closed = result_seen = False
    correction_started = correction_closed = correction_result = False
    for event in events:
        _require(isinstance(event, dict), 'invalid_event_object')
        kind, subtype = event.get('type'), event.get('subtype')
        _require(isinstance(kind, str) and bool(kind), 'invalid_event_type')
        blocks = _blocks(event) if kind in ('assistant', 'user') else []
        if event.get('parent_tool_use_id') is not None:
            _require(isinstance(event['parent_tool_use_id'], str)
                     and bool(event['parent_tool_use_id']), 'invalid_parent_identity')
            _require(not any(b.get('type') == 'tool_use' and b.get('name') in
                             ('Agent', 'Task', 'SendMessage') for b in blocks), 'nested_dispatch')
            continue
        _require(event.get('session_id', intent['session_id']) == intent['session_id'],
                 'session_mismatch')
        if trailing:
            _require(kind == 'result' and subtype == 'error_max_budget_usd',
                     'activity_after_budget_reinitialization')
        if kind == 'system' and subtype == 'init':
            _require(not initialized or (terminal and saw_stop and not trailing),
                     'duplicate_initialization')
            trailing = initialized
            _require(event.get('session_id') == intent['session_id'], 'session_mismatch')
            _require(event.get('model') == intent['reported_model'], 'model_mismatch')
            _require(event.get('claude_code_version') == intent['cli_version'],
                     'cli_version_mismatch')
            _require(event.get('cwd') == intent['cwd'], 'cwd_mismatch')
            initialized = True
        elif kind == 'result':
            _require(event.get('session_id') == intent['session_id'], 'session_mismatch')
            stopped = subtype == 'error_max_budget_usd' and event.get('is_error') is True
            _require(stopped or (subtype == 'success' and event.get('is_error') is False),
                     'main_result_failed')
            saw_stop |= stopped
            terminal = closed and result_seen and (correction is None or (
                correction_started and correction_closed and correction_result))
        elif kind in ('assistant', 'user', 'stream_event'):
            terminal = False
        for block in blocks:
            if block.get('type') == 'tool_use':
                _require(kind == 'assistant' and initialized, 'invalid_tool_role')
                name, content = block.get('name'), block.get('input')
                _require(name != 'Task', 'extra_dispatch')
                if name not in ('Agent', 'SendMessage'):
                    continue
                _require(isinstance(content, dict), 'invalid_creation_input')
                if name == 'Agent':
                    _require(tool is None and correction is None, 'extra_dispatch')
                    _require(equivalent_message(content.get('prompt'), step['message']),
                             'initialization_message_mismatch')
                    _require(content.get('subagent_type') == step['member_id'],
                             'native_type_mismatch')
                    _require('model' not in content, 'creation_model_override')
                    _require(content.get('run_in_background') is False
                             and 'resume' not in content, 'invalid_creation_mode')
                    tool = block.get('id')
                    _require(_safe_id(tool), 'invalid_tool_identity')
                else:
                    _require(correction is None and closed and result_seen, 'extra_dispatch')
                    recipients = [content[k] for k in ('to', 'recipient') if k in content]
                    messages = [content[k] for k in ('message', 'content') if k in content]
                    _require(recipients and all(v == agent for v in recipients),
                             'correction_recipient_mismatch')
                    _require(messages and all(isinstance(v, str) and v for v in messages),
                             'invalid_correction_message')
                    _require('type' not in content or content['type'] == 'message',
                             'invalid_correction_type')
                    correction = block.get('id')
                    _require(_safe_id(correction) and correction != tool,
                             'invalid_correction_identity')
            elif block.get('type') == 'tool_result':
                _require(kind == 'user', 'invalid_tool_result_role')
                observed = block.get('tool_use_id')
                if correction is not None and observed == correction:
                    _require(not correction_result, 'duplicate_correction_result')
                    if block.get('is_error', False) is False:
                        content = block.get('content')
                        _require(isinstance(content, list) and len(content) == 1
                                 and content[0].get('type') == 'text', 'invalid_correction_result')
                        resumed = _json_object(content[0].get('text'))
                        _require(resumed.get('success') is True
                                 and resumed.get('resumedAgentId') == agent,
                                 'correction_identity_mismatch')
                    else:
                        _require(block.get('is_error') is True, 'invalid_correction_result')
                    correction_result = True
                else:
                    _require(tool is not None and observed == tool and not result_seen,
                             'tool_identity_mismatch')
                    _require(block.get('is_error', False) is False, 'creation_failed')
                    content = block.get('content')
                    _require(isinstance(content, list) and bool(content)
                             and all(isinstance(c, dict) and c.get('type') == 'text'
                                     and isinstance(c.get('text'), str) for c in content),
                             'invalid_creation_result')
                    result_seen = True
        if kind == 'system' and subtype in ('task_started', 'task_notification'):
            _require(initialized and tool is not None, 'invalid_task_order')
            _require(event.get('session_id') == intent['session_id'], 'session_mismatch')
            _require('subagent_type' not in event
                     or event['subagent_type'] == step['member_id'], 'native_type_mismatch')
            is_correction = correction is not None and event.get('tool_use_id') == correction
            _require(is_correction or event.get('tool_use_id') == tool, 'task_identity_mismatch')
            if subtype == 'task_started':
                _require(type(event.get('spawn_depth')) is int and event['spawn_depth'] == 1
                         and event.get('task_type') == 'local_agent', 'invalid_task_kind')
                if is_correction:
                    _require(not correction_started and event.get('task_id') == agent,
                             'correction_identity_mismatch')
                    correction_started = True
                else:
                    _require(not started and event.get('subagent_type') == step['member_id'],
                             'duplicate_task_started')
                    agent = event.get('task_id')
                    _require(_safe_id(agent), 'invalid_agent_identity')
                    started = True
            else:
                _require(event.get('task_id') == agent, 'task_identity_mismatch')
                if is_correction:
                    _require(correction_started and not correction_closed
                             and event.get('status') in ('completed', 'stopped', 'failed'),
                             'pending_task_not_closed')
                    correction_closed = True
                else:
                    _require(started and not closed and event.get('status') == 'completed',
                             'pending_task_not_closed')
                    closed = True
    _require(terminal and closed and result_seen and saw_stop is budget_stop,
             'missing_creation_evidence')
    return {'agent_id': agent, 'creation_tool_use_id': tool,
            'stdout_sha256': sha(data['stdout']), 'parent_budget_stopped': budget_stop,
            'handshake_verified': False}


def _prefix(step, attempt):
    return 'members/' + step['member_id'] + '/repairs/' + str(UUID(attempt)) + '/'


def _message(step, attempt):
    payload = {'operation': 'repair_initialization_handshake', 'repair_attempt_id': attempt,
               'task_result': {k: step[k] for k in
                               ('initialization_sha256', 'member_id', 'role')}}
    digest = sha(json_bytes(payload))
    message = json_bytes(payload | {'required_response': {
        'delivery_sha256': digest, 'task_result': payload['task_result']}}).decode()
    return message, digest


def _request(intent, step, repair):
    return replace(init._as_request(intent, step), attempt_id=repair['attempt_id'],
                   prompt=repair['prompt'], budget_usd=Decimal(repair['budget_usd']),
                   timeout_seconds=repair['timeout_seconds'],
                   max_output_bytes=repair['max_output_bytes'], resume=True,
                   create_agent=False, bare=False)


def _validate_repair(intent, step, repair):
    message, digest = _message(step, repair['attempt_id'])
    _require(repair['initialization_sha256'] == sha(json_bytes(intent))
             and repair['member_id'] == step['member_id']
             and repair['message'] == message and repair['delivery_sha256'] == digest,
             'repair_intent_invalid')
    _require(repair['prompt'] == _prompt(repair['agent_id'], message), 'repair_intent_invalid')
    build_command(_request(intent, step, repair))


def _prompt(agent, message):
    arguments = {'to': agent, 'message': message, 'summary': 'Repair initialization handshake'}
    return ('Send exactly one SendMessage with the exact canonical arguments below. '
            'Use only to/message/summary keys; omit legacy content/recipient/type. '
            'Never create an Agent or dispatch another child. Wait for completion. '
            'The child must copy required_response as its entire JSON response.\n'
            + json_bytes(arguments).decode())


def _repair_transport(intent, step, repair, data):
    expected = init._record_config(_request(intent, step, repair)) | {
        'cli_state_dir': intent['cli_state_dir']}
    saved = loads(data['request'].decode())
    _require(all(saved.get(k) == v for k, v in expected.items()), 'transport_request_changed')
    outcome = loads(data['outcome'].decode())
    _require(outcome.get('session_id') == intent['session_id']
             and outcome.get('attempt_id') == repair['attempt_id']
             and outcome.get('stdout_sha256') == sha(data['stdout'])
             and type(outcome.get('returncode')) is int and outcome['returncode'] == 0
             and outcome.get('status') == 'transport_succeeded', 'repair_transport_failed')


def _original_data(video, intent, step):
    attempt = safe_child(video, 'sessions/cli-calls/' + intent['session_id'] + '/'
                         + step['attempt_id'])
    return {k: safe_child(attempt, filename).read_bytes() for k, filename in init.FILES.items()}


def _original_refs(base, step, data):
    prefix = 'members/' + step['member_id'] + '/'
    return {k: init._save(base, prefix + init.FILES[k], raw) for k, raw in data.items()}


def _read_original(base, step, refs):
    prefix = 'members/' + step['member_id'] + '/'
    return {k: init._read_ref(base, refs[k], prefix + name) for k, name in init.FILES.items()}


def _admission(base, intent, repair):
    spent = largest = Decimal(0)
    overrun = False
    observed = set()
    index = next(i for i, s in enumerate(intent['steps'])
                 if s['member_id'] == repair['member_id'])
    _require([e['member_id'] for e in repair['prior_calls']] ==
             [s['member_id'] for s in intent['steps'][:index + 1]],
             'repair_accounting_invalid')
    prior_ids = [e['member_id'] for e in repair['prior_repairs']]
    _require(len(prior_ids) == len(set(prior_ids))
             and set(prior_ids) <= {s['member_id'] for s in intent['steps'][:index]},
             'repair_accounting_invalid')
    for entry in repair['prior_calls']:
        step = next(s for s in intent['steps'] if s['member_id'] == entry['member_id'])
        _require(step['attempt_id'] not in observed, 'repair_accounting_invalid')
        observed.add(step['attempt_id'])
        data = _read_original(base, step, entry['files'])
        birth = inspect_pending_birth(intent, step, data)
        cost = init._cost(data['stdout'], intent['session_id'])
        spent += cost
        largest = max(largest, cost)
        overrun |= cost > Decimal(step['budget_usd']) or birth['parent_budget_stopped']
    # Previous successful repairs are validated and counted separately by their frozen proofs.
    for previous in repair['prior_repairs']:
        step = next(s for s in intent['steps'] if s['member_id'] == previous['member_id'])
        pointer = safe_child(base, 'members/' + step['member_id'] + '/repair_ref.json')
        _require(loads(pointer.read_text(encoding='utf-8')) == previous['proof_ref'],
                 'repair_accounting_invalid')
        _, cost, quota = load_repair(base, intent, step, previous['proof_ref'])
        spent += cost
        largest = max(largest, cost)
        overrun |= cost > quota
    expected_prior = [s['member_id'] for s in intent['steps'][:index]
                      if safe_child(base, 'members/' + s['member_id']
                                    + '/repair_ref.json').exists()]
    _require(prior_ids == expected_prior, 'repair_accounting_invalid')
    remaining = sum((Decimal(s['budget_usd']) for s in intent['steps']
                     if s['attempt_id'] not in observed), Decimal(0))
    needed = remaining + Decimal(repair['budget_usd'])
    if overrun:
        needed = max(needed, 2 * largest)
    limit = init._load_spend(base, intent, repair['spending_authorization_ref'])
    _require(spent + needed <= limit, 'blocked_budget_headroom')
    return spent, largest, overrun, limit


def load_repair(base, intent, step, ref):
    prefix = _prefix(step, ref['path'].split('/')[-2])
    proof = loads(init._read_ref(base, ref, prefix + 'proof.json').decode())
    repair = loads(init._read_ref(base, proof['intent_ref'], prefix + 'intent.json').decode())
    _validate_repair(intent, step, repair)
    original = _read_original(base, step, proof['original_files'])
    birth = inspect_pending_birth(intent, step, original)
    _require(birth == proof['birth'] and birth['agent_id'] == repair['agent_id'],
             'repair_identity_mismatch')
    data = {k: init._read_ref(base, proof['files'][k], prefix + name)
            for k, name in init.FILES.items()}
    _repair_transport(intent, step, repair, data)
    native = verify_delivery(data['stdout'], session_id=intent['session_id'],
                             agent_id=birth['agent_id'], message=repair['message'],
                             delivery_sha256=repair['delivery_sha256'],
                             expected_model=intent['reported_model'],
                             cli_version=intent['cli_version'], cwd=intent['cwd'])
    _require(native == proof['delivery'] and native['task_result'] == {
        k: step[k] for k in ('initialization_sha256', 'member_id', 'role')}, 'handshake_mismatch')
    spent, _, _, limit = _admission(base, intent, repair)
    cost = init._cost(data['stdout'], intent['session_id'])
    _require(spent + cost <= limit, 'initialization_spend_limit')
    summary = {k: birth[k] for k in
               ('agent_id', 'creation_tool_use_id', 'stdout_sha256', 'parent_budget_stopped')}
    return summary | {'acknowledgement_repaired': True}, cost, Decimal(repair['budget_usd'])


def selected_repair(base, intent, step):
    path = safe_child(base, 'members/' + step['member_id'] + '/repair_ref.json')
    if not path.exists():
        return None
    ref = loads(path.read_text(encoding='utf-8'))
    return ref, load_repair(base, intent, step, ref)


def validate_repair_history(base, intent):
    """Incomplete repair intents cannot disappear from subsequent spend admission."""
    known = set()
    for step in intent['steps']:
        directory = safe_child(base, 'members/' + step['member_id'] + '/repairs')
        if not directory.exists():
            continue
        for path in directory.iterdir():
            _require(path.is_dir(), 'repair_history_invalid')
            selected = selected_repair(base, intent, step)
            _require(selected is not None and selected[0]['path'] ==
                     _prefix(step, path.name) + 'proof.json', 'repair_history_incomplete')
            _require(path.name not in known, 'repair_attempt_conflict')
            known.add(path.name)
    return known


def validate_initialization_attempts(video, base, intent):
    known = validate_repair_history(base, intent)
    known.update(s['attempt_id'] for s in intent['steps'])
    if 'recovery_ref' in intent:
        ref = intent['recovery_ref']
        old = loads(init._read_ref(base, ref['intent'],
                    'history/' + ref['initialization_id'] + '/intent.json').decode())
        known.add(old['steps'][0]['attempt_id'])
    records = safe_child(video, 'sessions/cli-calls/' + intent['session_id'])
    if records.exists():
        _require(all(not p.is_dir() or p.name in known for p in records.iterdir()),
                 'unmanaged_initialization_attempt')


def _resume_refusal(intent, repair, data):
    """Diagnose a correlated native refusal only; never manufacture delivery evidence."""
    checked = parse_events(data['stdout'], session_id=intent['session_id'], returncode=0)
    _require(checked['status'] == 'transport_succeeded', 'repair_transport_failed')
    events = [loads(line) for line in data['stdout'].decode().splitlines() if line.strip()]
    initialized = False
    tool = None
    refused = result_seen = started = False
    for event in events:
        kind, subtype = event.get('type'), event.get('subtype')
        blocks = _blocks(event) if kind in ('assistant', 'user') else []
        if event.get('parent_tool_use_id') is not None:
            _require(not any(b.get('type') == 'tool_use' and b.get('name') in
                             ('Agent', 'Task', 'SendMessage') for b in blocks), 'nested_dispatch')
            continue
        _require(event.get('session_id', intent['session_id']) == intent['session_id'],
                 'session_mismatch')
        if kind == 'system' and subtype == 'init':
            _require(tool is None, 'duplicate_initialization')
            _require(event.get('model') == intent['reported_model']
                     and event.get('claude_code_version') == intent['cli_version']
                     and event.get('cwd') == intent['cwd'], 'repair_identity_mismatch')
            initialized = True
        if (kind == 'system' and subtype == 'task_started'
                and tool is not None and event.get('tool_use_id') == tool):
            started = True
        for block in blocks:
            if block.get('type') == 'tool_use':
                _require(block.get('name') not in ('Agent', 'Task'), 'unexpected_agent_creation')
                if block.get('name') != 'SendMessage':
                    continue
                _require(initialized and kind == 'assistant' and tool is None, 'extra_dispatch')
                _require(event.get('session_id') == intent['session_id'], 'session_mismatch')
                content = block.get('input')
                _require(isinstance(content, dict), 'invalid_dispatch_input')
                recipients = [content[k] for k in ('to', 'recipient') if k in content]
                # Diagnostic only: CLI may truncate the legacy display alias.
                # Positive delivery still validates every alias in verify_delivery.
                message = content.get('message') if 'message' in content else content.get('content')
                _require(recipients and all(v == repair['agent_id'] for v in recipients),
                         'recipient_mismatch')
                _require(equivalent_message(message, repair['message']),
                         'delivery_message_mismatch')
                _require('type' not in content or content['type'] == 'message',
                         'invalid_dispatch_type')
                tool = block.get('id')
                _require(_safe_id(tool), 'invalid_tool_identity')
            elif (block.get('type') == 'tool_result' and tool is not None
                  and block.get('tool_use_id') == tool):
                _require(kind == 'user' and not result_seen, 'tool_identity_mismatch')
                result_seen = True
                _require(event.get('session_id') == intent['session_id'], 'session_mismatch')
                content = block.get('content')
                _require(isinstance(content, list) and len(content) == 1
                         and isinstance(content[0], dict) and content[0].get('type') == 'text',
                         'invalid_resume_result')
                result = _json_object(content[0].get('text'))
                if result.get('success') is False:
                    _require(not refused and block.get('is_error', False) is False,
                             'invalid_resume_result')
                    refused = True
    _require(not refused or not started, 'contradictory_resume_evidence')
    return refused


def _refusal_diagnostic(intent, repair, data):
    if not _resume_refusal(intent, repair, data):
        return None
    try:
        cost = init._cost(data['stdout'], intent['session_id'])
    except ValueError:
        cost = None
    return {'error_code': 'member_resume_refused', 'agent_id': repair['agent_id'], 'cost': cost}


def repair_failure(base, intent, step):
    """Read completed repair refusal evidence without accepting membership or writing files."""
    init._validate_intent(intent)
    init._validate_recovery(base, intent)
    _require(step in intent['steps'], 'repair_identity_mismatch')
    directory = safe_child(base, 'members/' + step['member_id'] + '/repairs')
    if not directory.exists():
        return None
    found = None
    for path in directory.iterdir():
        _require(path.is_dir(), 'repair_history_invalid')
        prefix = _prefix(step, path.name)
        repair = loads(safe_child(base, prefix + 'intent.json').read_text(encoding='utf-8'))
        _validate_repair(intent, step, repair)
        _require(repair['attempt_id'] == path.name, 'repair_intent_invalid')
        init._load_spend(base, intent, repair['spending_authorization_ref'])
        original = next((e for e in repair['prior_calls']
                         if e['member_id'] == step['member_id']), None)
        _require(original is not None, 'repair_intent_invalid')
        birth = inspect_pending_birth(intent, step, _read_original(base, step, original['files']))
        _require(birth['agent_id'] == repair['agent_id'], 'repair_identity_mismatch')
        try:
            data = {k: safe_child(base, prefix + name).read_bytes()
                    for k, name in init.FILES.items()}
        except OSError:
            continue
        _repair_transport(intent, step, repair, data)
        diagnostic = _refusal_diagnostic(intent, repair, data)
        if diagnostic is not None:
            _require(found is None, 'repair_history_invalid')
            found = diagnostic
    return found


def _same_repair_request(base, intent, step, repair, request, spend_limit):
    _validate_repair(intent, step, repair)
    supplied = replace(request, prompt=repair['prompt'], resume=True,
                       agents=step['agents'], create_agent=False, bare=False)
    _require(init._record_config(supplied) == init._record_config(_request(intent, step, repair))
             and init._load_spend(base, intent, repair['spending_authorization_ref']) == spend_limit,
             'repair_intent_conflict')


def _finish_repair(video, base, intent, step, repair, intent_ref, original, birth, *, reused):
    """Verify saved process output and publish deterministic proof without launching anything."""
    prefix = _prefix(step, repair['attempt_id'])
    attempt = safe_child(video, 'sessions/cli-calls/' + intent['session_id'] + '/'
                         + repair['attempt_id'])
    try:
        data = {k: safe_child(attempt, name).read_bytes() for k, name in init.FILES.items()}
    except OSError:
        return {'status': 'blocked', 'error_code': 'repair_records_incomplete', 'reused': reused}
    refs = {k: init._save(base, prefix + init.FILES[k], raw) for k, raw in data.items()}
    try:
        _require(birth['agent_id'] == repair['agent_id'], 'repair_identity_mismatch')
        _repair_transport(intent, step, repair, data)
        refusal = _refusal_diagnostic(intent, repair, data)
        if refusal is not None:
            cost = refusal['cost']
            return {'status': 'blocked', 'error_code': refusal['error_code'],
                    'agent_id': refusal['agent_id'], 'reused': reused,
                    'actual_estimated_cost_usd': str(cost) if cost is not None else None}
        delivery = verify_delivery(data['stdout'], session_id=intent['session_id'],
            agent_id=birth['agent_id'], message=repair['message'],
            delivery_sha256=repair['delivery_sha256'], expected_model=intent['reported_model'],
            cli_version=intent['cli_version'], cwd=intent['cwd'])
        _require(delivery['task_result'] == {k: step[k] for k in
                 ('initialization_sha256', 'member_id', 'role')}, 'handshake_mismatch')
        cost = init._cost(data['stdout'], intent['session_id'])
        spent, _, _, limit = _admission(base, intent, repair)
        _require(spent + cost <= limit, 'initialization_spend_limit')
    except ValueError as exc:
        return {'status': 'blocked', 'error_code': str(exc), 'reused': reused}
    proof = {'intent_ref': intent_ref, 'birth': birth, 'delivery': delivery, 'files': refs,
             'original_files': _original_refs(base, step, original)}
    ref = init._save(base, prefix + 'proof.json', json_bytes(proof))
    load_repair(base, intent, step, ref)
    init._save(base, 'members/' + step['member_id'] + '/repair_ref.json', json_bytes(ref))
    return {'status': 'repaired', 'agent_id': birth['agent_id'], 'proof_ref': ref,
            'actual_estimated_cost_usd': str(cost), 'reused': reused}


def repair_member_handshake(video: Path, member_id: str, *, request: CliRequest,
                            environment: dict, spend_limit: Decimal, authorized=False) -> dict:
    if authorized is not True:
        raise ValueError('execution_not_authorized')
    _require(isinstance(spend_limit, Decimal) and spend_limit.is_finite()
             and spend_limit > 0, 'initialization_spend_limit_invalid')
    require_ignored(video)
    base = safe_child(video, 'sessions/team')
    with preparation_lock(safe_child(video, 'sessions/team.lock')):
        intent = loads(safe_child(base, 'intent.json').read_text(encoding='utf-8'))
        init._validate_intent(intent)
        init._validate_recovery(base, intent)
        step = next((s for s in intent['steps'] if s['member_id'] == member_id), None)
        _require(step is not None, 'unknown_initialization_member')
        _require(request.cwd.resolve() == video.resolve()
                 and str(video) == intent['cwd'] and request.session_id == intent['session_id']
                 and request.model == intent['configured_model']
                 and str(request.executable) == intent['executable'], 'repair_identity_mismatch')
        state = str(safe_child(video, 'sessions/claude-state'))
        _require(isinstance(environment, dict) and all(
            isinstance(k, str) and k and '=' not in k and '\0' not in k
            and isinstance(v, str) and '\0' not in v for k, v in environment.items()),
            'invalid_execution_environment')
        _require(intent['cli_state_dir'] == state
                 and environment.get('CLAUDE_CONFIG_DIR', state) == state, 'cli_state_dir_conflict')
        original = _original_data(video, intent, step)
        selected = selected_repair(base, intent, step)
        if selected:
            ref, (native, cost, _) = selected
            if ref['path'] == _prefix(step, request.attempt_id) + 'proof.json':
                repair = loads(safe_child(base, _prefix(step, request.attempt_id)
                                         + 'intent.json').read_text(encoding='utf-8'))
                _same_repair_request(base, intent, step, repair, request, spend_limit)
            init._transport(intent, step, original, allow_budget_stop=True)
            _require(native['stdout_sha256'] == sha(original['stdout']), 'team_hash_mismatch')
            return {'status': 'repaired', 'agent_id': native['agent_id'], 'proof_ref': ref,
                    'actual_estimated_cost_usd': str(cost), 'reused': True}
        try:
            native = init._native(intent, step, original)
        except ValueError:
            native = None
        if native is not None:
            cost = init._cost(original['stdout'], intent['session_id'])
            refs = _original_refs(base, step, original)
            proof = {'native': native, 'message': step['message'], 'files': refs}
            ref = init._save(base, 'members/' + member_id + '/proof.json', json_bytes(proof))
            return {'status': 'repaired', 'agent_id': native['agent_id'], 'proof_ref': ref,
                    'reused': True, 'actual_estimated_cost_usd': str(cost)}
        birth = inspect_pending_birth(intent, step, original)
        prefix = _prefix(step, request.attempt_id)
        existing = safe_child(base, prefix + 'intent.json')
        if existing.exists():
            raw = existing.read_bytes()
            repair = loads(raw.decode('utf-8'))
            _same_repair_request(base, intent, step, repair, request, spend_limit)
            intent_ref = {'path': prefix + 'intent.json', 'sha256': sha(raw)}
            return _finish_repair(video, base, intent, step, repair, intent_ref, original,
                                  birth, reused=True)
        prior_calls = []
        known = set()
        for prior in intent['steps']:
            attempt = safe_child(video, 'sessions/cli-calls/' + intent['session_id'] + '/'
                                 + prior['attempt_id'])
            known.add(prior['attempt_id'])
            if attempt.exists():
                data = _original_data(video, intent, prior)
                inspect_pending_birth(intent, prior, data)
                init._cost(data['stdout'], intent['session_id'])
                prior_calls.append({'member_id': prior['member_id'],
                                    'files': _original_refs(base, prior, data)})
        prior_repairs = []
        for prior in intent['steps']:
            directory = safe_child(base, 'members/' + prior['member_id'] + '/repairs')
            if not directory.exists():
                continue
            for path in directory.iterdir():
                _require(path.is_dir(), 'repair_history_invalid')
                chosen = selected_repair(base, intent, prior)
                _require(chosen is not None
                         and chosen[0]['path'] == _prefix(prior, path.name) + 'proof.json',
                         'repair_history_incomplete')
                known.add(path.name)
                prior_repairs.append({'member_id': prior['member_id'], 'proof_ref': chosen[0]})
        if 'recovery_ref' in intent:
            oldref = intent['recovery_ref']
            old = loads(init._read_ref(base, oldref['intent'],
                        'history/' + oldref['initialization_id'] + '/intent.json').decode())
            known.add(old['steps'][0]['attempt_id'])
        records = safe_child(video, 'sessions/cli-calls/' + intent['session_id'])
        _require(all(not p.is_dir() or p.name in known for p in records.iterdir()),
                 'unmanaged_initialization_attempt')
        _require(request.attempt_id not in known, 'repair_attempt_conflict')
        message, digest = _message(step, request.attempt_id)
        repair = {'initialization_sha256': sha(json_bytes(intent)), 'member_id': member_id,
                  'agent_id': birth['agent_id'], 'attempt_id': request.attempt_id,
                  'budget_usd': str(request.budget_usd), 'timeout_seconds': request.timeout_seconds,
                  'max_output_bytes': request.max_output_bytes, 'message': message,
                  'delivery_sha256': digest, 'prompt': _prompt(birth['agent_id'], message),
                  'prior_calls': prior_calls, 'prior_repairs': prior_repairs,
                  'spending_authorization_ref': init._spend_authorization(base, intent, spend_limit)}
        _validate_repair(intent, step, repair)
        _admission(base, intent, repair)
        intent_ref = init._save(base, prefix + 'intent.json', json_bytes(repair))
        current = _request(intent, step, repair)
        init.execute(current, records_root=safe_child(video, 'sessions/cli-calls'),
                     environment=dict(environment, CLAUDE_CONFIG_DIR=state), authorized=True)
        return _finish_repair(video, base, intent, step, repair, intent_ref, original,
                              birth, reused=False)
