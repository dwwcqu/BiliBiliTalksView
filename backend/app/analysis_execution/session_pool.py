"""Immutable ordinary CLI identities and conservative, durable turn accounting."""

import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import require_ignored, safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_results.acceptance import _write_once

from .command import build_command
from .events import parse_events
from .runner import execute
from .session_evidence import verify_session_output

ROLES = {'user_initial', 'thread_context', 'user_synthesis'}


def _amount(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError('pool_budget_invalid')
    return value


def _members(members):
    if (not isinstance(members, list) or not members
            or any(not isinstance(m, dict)
                   or set(m) != {'member_id', 'role', 'role_sha256'}
                   or not isinstance(m['member_id'], str)
                   or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', m['member_id'])
                   or m['member_id'] == 'main' or m['role'] not in ROLES
                   or not isinstance(m['role_sha256'], str)
                   or not re.fullmatch('[0-9a-f]{64}', m['role_sha256']) for m in members)
            or len({m['member_id'] for m in members}) != len(members)):
        raise ValueError('invalid_pool_members')


def load_session_pool(video):
    video = Path(video).absolute()
    try:
        pool = loads(safe_child(video, 'sessions/pool/pool.json').read_text(encoding='utf-8'))
        expected = {'schema_version', 'video_id', 'model', 'provider', 'main', 'members',
                    'cli_state_dir', 'created_at', 'prior_estimated_cost_usd', 'legacy'}
        if not isinstance(pool, dict) or set(pool) != expected:
            raise ValueError('pool_invalid')
        _members([{k: m[k] for k in ('member_id', 'role', 'role_sha256')}
                  for m in pool['members']])
        identities = [pool['main'], *pool['members']]
        if (pool['schema_version'] != '1.0.0'
                or pool['main'] != {'member_id': 'main', 'role': 'coordinator',
                                    'agent_id': pool['main']['agent_id']}
                or any(set(m) != {'member_id', 'role', 'role_sha256', 'agent_id'}
                       for m in pool['members'])
                or any(str(UUID(m['agent_id'])) != m['agent_id'] for m in identities)
                or len({m['agent_id'] for m in identities}) != len(identities)
                or any(not isinstance(pool[k], str) or not pool[k].strip()
                       for k in ('video_id', 'model', 'provider', 'created_at'))
                or pool['cli_state_dir'] != str(safe_child(video, 'sessions/pool-state'))
                or (pool['legacy'] is not None and not isinstance(pool['legacy'], dict))):
            raise ValueError('pool_invalid')
        _amount(Decimal(pool['prior_estimated_cost_usd']))
        datetime.fromisoformat(pool['created_at'])
        return pool
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError('pool_invalid') from exc


def create_session_pool(video, *, video_id, model, provider, members,
                        prior_estimated_cost_usd=Decimal(0), legacy=None):
    _members(members)
    _amount(prior_estimated_cost_usd)
    if (any(not isinstance(v, str) or not v.strip() for v in (video_id, model, provider))
            or (legacy is not None and not isinstance(legacy, dict))):
        raise ValueError('pool_config_invalid')
    video = Path(video).absolute()
    require_ignored(video)
    base = safe_child(video, 'sessions/pool')
    base.mkdir(parents=True, exist_ok=True)
    config = {'video_id': video_id, 'model': model, 'provider': provider,
              'prior_estimated_cost_usd': str(prior_estimated_cost_usd), 'legacy': legacy}
    with preparation_lock(safe_child(base, 'pool.lock')):
        if safe_child(base, 'pool.json').exists():
            pool = load_session_pool(video)
            if (any(pool[k] != v for k, v in config.items())
                    or [{k: m[k] for k in ('member_id', 'role', 'role_sha256')}
                        for m in pool['members']] != members):
                raise ValueError('pool_configuration_conflict')
            return pool
        pool = config | {
            'schema_version': '1.0.0', 'created_at': datetime.now(UTC).isoformat(),
            'main': {'member_id': 'main', 'role': 'coordinator', 'agent_id': str(uuid4())},
            'members': [m | {'agent_id': str(uuid4())} for m in members],
            'cli_state_dir': str(safe_child(video, 'sessions/pool-state')),
        }
        _write_once(safe_child(base, 'pool.json'), json_bytes(pool))
        return load_session_pool(video)


def _config(request, state):
    prompt = request.prompt.encode('utf-8')
    return {'session_id': request.session_id, 'attempt_id': request.attempt_id,
            'resume': request.resume, 'configured_model': request.model,
            'executable': str(request.executable), 'cwd': str(request.cwd),
            'prompt_sha256': sha(prompt), 'prompt_bytes': len(prompt),
            'budget_usd': str(request.budget_usd), 'timeout_seconds': request.timeout_seconds,
            'max_output_bytes': request.max_output_bytes, 'agents_sha256': None,
            'create_agent': False, 'bare': True, 'direct_session': True, 'cli_state_dir': state}


def _read_attempt(path):
    saved = loads(safe_child(path, 'request.json').read_text(encoding='utf-8'))
    outcome = loads(safe_child(path, 'outcome.json').read_text(encoding='utf-8'))
    raw = safe_child(path, 'stdout.jsonl').read_bytes()
    stderr = safe_child(path, 'stderr.txt').read_bytes()
    if (not isinstance(saved, dict) or not isinstance(outcome, dict)
            or saved.get('session_id') != path.parent.name
            or saved.get('attempt_id') != path.name
            or outcome.get('session_id') != path.parent.name
            or outcome.get('attempt_id') != path.name
            or outcome.get('stdout_sha256') != sha(raw)
            or outcome.get('stderr_sha256') != sha(stderr)
            or type(outcome.get('returncode')) is not int
            or outcome.get('error_code') == 'process_cleanup_unverified'):
        raise ValueError('pool_call_invalid')
    parsed = parse_events(raw, session_id=path.parent.name, returncode=outcome['returncode'])
    if any(outcome.get(k) != v for k, v in parsed.items()):
        raise ValueError('pool_call_invalid')
    return saved, outcome, raw


def _cost(raw, session_id):
    events = [loads(line) for line in raw.decode('utf-8').split('\n') if line.strip()]
    if any(not isinstance(e, dict) for e in events):
        raise ValueError('pool_cost_unknown')
    results = [e for e in events if e.get('type') == 'result'
               and e.get('parent_tool_use_id') is None]
    if len(results) != 1 or results[0].get('session_id') != session_id:
        raise ValueError('pool_cost_unknown')
    value = results[0].get('total_cost_usd')
    if type(value) not in (Decimal, int):
        raise ValueError('pool_cost_unknown')
    return _amount(Decimal(value))


def _ledger(records, pool):
    spent = Decimal(pool['prior_estimated_cost_usd'])
    largest = Decimal(0)
    overrun = False
    registered = {m['agent_id'] for m in [pool['main'], *pool['members']]}
    if not records.exists():
        return spent, largest, overrun
    for directory in records.iterdir():
        directory = safe_child(records, directory.name)
        if not directory.is_dir() or directory.name not in registered:
            raise ValueError('pool_unmanaged_call')
        for path in directory.iterdir():
            if path.name == 'execution.lock' and path.is_file():
                continue
            path = safe_child(directory, path.name)
            if not path.is_dir() or str(UUID(path.name)) != path.name:
                raise ValueError('pool_unmanaged_call')
            saved, _, raw = _read_attempt(path)
            authorization = loads(safe_child(path, 'pool-authorization.json').read_text('utf-8'))
            if (not isinstance(authorization, dict)
                    or authorization.get('request') != {k: v for k, v in saved.items()
                                                        if k != 'created_at'}
                    or authorization.get('pool_sha256') != sha(json_bytes(pool))):
                raise ValueError('pool_request_changed')
            cost = _cost(raw, directory.name)
            quota = _amount(Decimal(saved['budget_usd']))
            spent += cost
            largest = max(largest, cost)
            overrun |= cost > quota
    return spent, largest, overrun


def run_pool_turn(video, member_id, *, request, environment, expected_model,
                  spend_limit, authorized=False):
    if authorized is not True:
        raise ValueError('execution_not_authorized')
    _amount(spend_limit)
    video = Path(video).absolute()
    pool = load_session_pool(video)
    members = [m for m in [pool['main'], *pool['members']] if m['member_id'] == member_id]
    if len(members) != 1:
        raise ValueError('pool_member_missing')
    if (request.session_id != members[0]['agent_id']
            or request.cwd.resolve() != video.resolve() or request.model != pool['model']
            or not isinstance(expected_model, str) or not expected_model.strip()
            or request.agents is not None or request.create_agent or not request.bare):
        raise ValueError('pool_request_invalid')
    if (not isinstance(environment, dict)
            or any(not isinstance(k, str) or not k or '=' in k or '\0' in k
                   or not isinstance(v, str) or '\0' in v for k, v in environment.items())):
        raise ValueError('invalid_execution_environment')
    state = pool['cli_state_dir']
    if environment.get('CLAUDE_CONFIG_DIR', state) != state:
        raise ValueError('cli_state_dir_conflict')
    environment = dict(environment, CLAUDE_CONFIG_DIR=state)
    records = safe_child(video, 'sessions/pool-calls')
    session = safe_child(records, members[0]['agent_id'])
    with preparation_lock(safe_child(video, 'sessions/pool/pool.lock')):
        current = replace(request, session_id=members[0]['agent_id'], direct_session=True,
                          resume=session.exists() and any(p.is_dir() for p in session.iterdir()))
        build_command(current)
        attempt = safe_child(session, current.attempt_id)
        reused = attempt.exists()
        if reused:
            saved = loads(safe_child(attempt, 'request.json').read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or type(saved.get('resume')) is not bool:
                raise ValueError('pool_request_changed')
            current = replace(current, resume=saved['resume'])
            if any(saved.get(k) != v for k, v in _config(current, state).items()):
                raise ValueError('pool_request_changed')
        try:
            if not reused:
                spent, largest, overrun = _ledger(records, pool)
                needed = max(current.budget_usd, 2 * largest) if overrun else current.budget_usd
                if spent + needed > spend_limit:
                    raise ValueError('pool_spend_limit')
                # execute reserves the attempt atomically; authorization is added before returning.
                execute(current, records_root=records, environment=environment, authorized=True)
                _write_once(safe_child(attempt, 'pool-authorization.json'), json_bytes({
                    'pool_sha256': sha(json_bytes(pool)), 'request': _config(current, state),
                    'spend_limit_usd': str(spend_limit), 'expected_model': expected_model,
                }))
            saved, outcome, raw = _read_attempt(attempt)
            auth = loads(safe_child(attempt, 'pool-authorization.json').read_text('utf-8'))
            if (auth.get('request') != _config(current, state)
                    or auth.get('expected_model') != expected_model
                    or auth.get('pool_sha256') != sha(json_bytes(pool))):
                raise ValueError('pool_request_changed')
            spent, _, _ = _ledger(records, pool)
            if spent > spend_limit:
                raise ValueError('pool_spend_limit')
            if outcome['status'] != 'transport_succeeded':
                raise ValueError('pool_transport_failed')
            result = verify_session_output(raw, session_id=current.session_id,
                                           expected_model=expected_model, cli_version='2.1.261',
                                           cwd=current.cwd)
            return {'status': 'succeeded', 'result': result, 'transport': outcome,
                    'request': current, 'records_path': str(attempt), 'reused': reused}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'status': 'blocked', 'error_code': str(exc), 'reused': reused,
                    'request': current, 'records_path': str(attempt)}
