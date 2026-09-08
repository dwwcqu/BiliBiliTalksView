"""Read-only partial initialization status; observed IDs never authorize dispatch."""

import re
from decimal import Decimal
from pathlib import Path

from app.analysis_input.storage import safe_child
from app.analysis_packets.codec import loads

from .events import equivalent_message
from .initialization import (
    FILES,
    _cost,
    _member_native,
    _transport,
    _validate_intent,
    _validate_recovery,
    load_team,
)


def _observed_ids(raw, intent, step):
    events = [loads(line) for line in raw.decode('utf-8').split('\n') if line.strip()]
    if any(not isinstance(e, dict) or e.get('session_id', intent['session_id'])
           != intent['session_id'] for e in events):
        return []
    calls = set()
    for event in events:
        if event.get('type') != 'assistant' or event.get('parent_tool_use_id') is not None:
            continue
        for block in event.get('message', {}).get('content', []):
            if not isinstance(block, dict) or block.get('type') != 'tool_use':
                continue
            value = block.get('input', {})
            if (block.get('name') == 'Agent' and isinstance(value, dict)
                    and value.get('subagent_type') == step['member_id']
                    and equivalent_message(value.get('prompt'), step['message'])):
                calls.add(block.get('id'))
    return sorted({e['task_id'] for e in events if e.get('type') == 'system'
                   and e.get('subtype') == 'task_started'
                   and e.get('parent_tool_use_id') is None
                   and e.get('tool_use_id') in calls
                   and e.get('subagent_type') == step['member_id']
                   and isinstance(e.get('task_id'), str)
                   and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', e['task_id'])})


def initialization_status(video: Path) -> dict:
    video = Path(video)
    base = safe_child(video, 'sessions/team')
    if safe_child(base, 'team.json').is_file():
        team = load_team(video)
        return team | {'status': 'initialized', 'initialization_complete': True,
                       'members': [m | {'state': 'verified', 'observed_agent_ids': [m['agent_id']]}
                                   for m in team['members']]}
    path = safe_child(base, 'intent.json')
    if not path.exists():
        return {'status': 'not_started', 'initialization_complete': False, 'members': []}
    intent = loads(path.read_text(encoding='utf-8'))
    _validate_intent(intent)
    _validate_recovery(base, intent)
    if Path(intent['cwd']).resolve() != video.resolve():
        raise ValueError('team_identity_mismatch')
    members = []
    known_cost = Decimal(0)
    cost_known = True
    reason = None
    known_attempts = {s['attempt_id'] for s in intent['steps']}
    if 'recovery_ref' in intent:
        old = loads(safe_child(base, intent['recovery_ref']['intent']['path']).read_text(
            encoding='utf-8'))
        known_attempts.add(old['steps'][0]['attempt_id'])
    from .member_repair import repair_failure
    for step in intent['steps']:
        row = {k: step[k] for k in ('member_id', 'role')}
        row.update(state='not_started', observed_agent_ids=[], reason_code=None)
        attempt = safe_child(video, f"sessions/cli-calls/{intent['session_id']}/{step['attempt_id']}")
        if attempt.exists():
            try:
                data = {k: safe_child(attempt, name).read_bytes() for k, name in FILES.items()}
                _transport(intent, step, data, allow_budget_stop=True)
                row['observed_agent_ids'] = _observed_ids(data['stdout'], intent, step)
                try:
                    known_cost += _cost(data['stdout'], intent['session_id'])
                except ValueError:
                    cost_known = False
                repair_ref = None
                try:
                    native, repair_ref, repair_costs = _member_native(base, intent, step, data)
                    known_cost += sum((cost for cost, _ in repair_costs), Decimal(0))
                    row.update(state='verified', agent_id=native['agent_id'])
                except ValueError as exc:
                    row.update(state='created_unverified' if row['observed_agent_ids'] else 'blocked',
                               reason_code=str(exc))
                directory = safe_child(base, 'members/' + step['member_id'] + '/repairs')
                if directory.exists():
                    entries = list(directory.iterdir())
                    if len(entries) != 1 or not entries[0].is_dir():
                        cost_known = False
                    if repair_ref is not None and len(entries) == 1 and entries[0].is_dir():
                        known_attempts.add(entries[0].name)
                    if repair_ref is None:
                        failure = repair_failure(base, intent, step)
                        if failure is None or failure['cost'] is None:
                            cost_known = False
                        else:
                            known_cost += failure['cost']
                        if failure is not None:
                            if len(entries) == 1 and entries[0].is_dir():
                                known_attempts.add(entries[0].name)
                            row.update(state='resume_refused', reason_code=failure['error_code'])
            except (OSError, ValueError, TypeError, KeyError):
                row.update(state='incomplete_record', reason_code='initialization_record_unavailable')
                cost_known = False
        else:
            directory = safe_child(base, 'members/' + step['member_id'] + '/repairs')
            if directory.exists():
                cost_known = False
                row.update(state='incomplete_record', reason_code='orphan_repair_evidence')
        members.append(row)
    records = safe_child(video, 'sessions/cli-calls/' + intent['session_id'])
    if records.exists() and any(p.is_dir() and p.name not in known_attempts
                                for p in records.iterdir()):
        cost_known = False
        reason = 'unmanaged_initialization_attempt'
    return {'status': 'blocked' if reason or any(
                m['state'] not in {'verified','not_started'} for m in members)
            else 'pending', 'initialization_complete': False, 'session_id': intent['session_id'],
            'members': members, 'reason_code': reason, 'active_estimated_cost_usd': str(known_cost) if cost_known else None}
