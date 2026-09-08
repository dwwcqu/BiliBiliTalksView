"""Bind application-managed ordinary sessions without rewriting native histories."""

from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid5

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_packets.publication import publish_assembly
from app.analysis_results.acceptance import _write_once, load_context, read_state

from .team_binding import _existing_publication

ADAPTER = 'claude-code-session-2.1.261-v1'


def require_ready(run, bundle):
    prepared = loads(safe_child(run.parent.parent,
                     f'runs/{bundle.prepared_run_id}/run.json').read_text(encoding='utf-8'))
    allowed = prepared['request']['allow_partial']
    if (prepared['status'] != 'ready' or type(allowed) is not bool
            or bundle.manifest['counts']['known_users'] == 0
            or (bundle.manifest['coverage']['status'] == 'partial' and not allowed)):
        raise ValueError('source_not_ready')


def prepare_session_pool(root, run_id, manifest_id, *, rule_catalog, model, provider,
                         members, prior_estimated_cost_usd=Decimal(0)):
    from .session_pool import create_session_pool

    run, bundle, assembly, _ = read_state(root, run_id, manifest_id, rule_catalog)
    require_ready(run, bundle)
    video = run.parent.parent
    role_sha = assembly.resources['role_prompt']['sha256']
    if (not isinstance(members, list) or any(not isinstance(m, dict)
            or set(m) != {'member_id', 'role'} for m in members)):
        raise ValueError('invalid_pool_members')
    definitions = [m | {'role_sha256': role_sha} for m in members]
    existing = safe_child(video, 'sessions/pool/pool.json')
    if existing.exists():
        from .session_pool import load_session_pool
        pool = load_session_pool(video)
        if [{k: m[k] for k in ('member_id', 'role')} for m in pool['members']] != members:
            raise ValueError('pool_configuration_conflict')
        definitions = [{k: m[k] for k in ('member_id', 'role', 'role_sha256')}
                       for m in pool['members']]
        return create_session_pool(video, video_id=bundle.video_id, model=model, provider=provider,
                                   members=definitions, legacy=pool['legacy'],
                                   prior_estimated_cost_usd=prior_estimated_cost_usd)
    registry = assembly.member_registry
    legacy = {'session_id': registry['main']['session_id'],
              'members': [{k: m[k] for k in ('member_id', 'agent_id', 'role')}
                          for m in registry['members']]}
    old = safe_child(video, 'sessions/team/intent.json')
    if old.exists():
        from .initialization_status import initialization_status
        status = initialization_status(video)
        legacy = {'session_id': status['session_id'], 'intent_sha256': sha(old.read_bytes()),
                  'members': [{'member_id': m['member_id'], 'role': m['role'],
                               'agent_ids': m['observed_agent_ids']} for m in status['members']]}
    return create_session_pool(video, video_id=bundle.video_id, model=model, provider=provider,
                               members=definitions, legacy=legacy,
                               prior_estimated_cost_usd=prior_estimated_cost_usd)


def pool_reference(video):
    path = 'sessions/pool/pool.json'
    return {'path': path, 'sha256': sha(safe_child(video, path).read_bytes())}


def bind_session_pool(root, run_id, manifest_id, rule_catalog):
    from .session_pool import load_session_pool

    run, _, _, _ = load_context(root, run_id, manifest_id)
    with preparation_lock(safe_child(run, '.dispatch.lock')):
        run, bundle, assembly, accepted = read_state(root, run_id, manifest_id, rule_catalog)
        require_ready(run, bundle)
        pool = load_session_pool(run.parent.parent)
        if pool['video_id'] != bundle.video_id:
            raise ValueError('pool_video_mismatch')
        old = assembly.member_registry
        session = pool['main']['agent_id']
        if old['main']['status'] == 'running' or any(m['status'] == 'running' for m in old['members']):
            raise ValueError('member_busy')
        same = old['main']['session_id'] == session
        if not same and safe_child(run, 'execution.json').exists():
            raise ValueError('pool_migration_requires_new_run')
        if accepted and not same:
            raise ValueError('accepted_run_requires_original_members')
        if not same and old['main']['session_id'] is not None:
            legacy = pool['legacy'] or {}
            prior_members = legacy.get('members', [])
            if (legacy.get('session_id') != old['main']['session_id']
                    or not isinstance(prior_members, list)):
                raise ValueError('pool_migration_source_mismatch')
            for member in old['members']:
                matches = [m for m in prior_members if isinstance(m, dict)
                           and m.get('member_id') == member['member_id']
                           and m.get('role') == member['role']
                           and (m.get('agent_id') == member['agent_id']
                                or member['agent_id'] in m.get('agent_ids', []))]
                if len(matches) != 1:
                    raise ValueError('pool_migration_source_mismatch')
        previous = {m['agent_id']: m for m in old['members']} if same else {}
        if same and set(previous) != {m['agent_id'] for m in pool['members']}:
            raise ValueError('pool_registry_conflict')
        members = []
        for member in pool['members']:
            row = {k: member[k] for k in ('member_id', 'agent_id', 'role', 'role_sha256')}
            row['role_sha256'] = assembly.resources['role_prompt']['sha256']
            prior = previous.get(member['agent_id'])
            if prior and any(prior[k] != row[k] for k in row):
                raise ValueError('pool_registry_conflict')
            members.append(row | {'parent_session_id': session, 'status': 'available',
                                  'active_task_id': None,
                                  'task_ids': list(prior['task_ids']) if prior else []})
        assigned = {tid for m in members for tid in m['task_ids']}
        offsets = {}
        for packet in sorted(assembly.packets, key=lambda p: p['task_id']):
            choices = [m for m in members if m['role'] == packet['task_type']]
            if not choices:
                raise ValueError('pool_role_missing')
            if packet['task_id'] not in assigned:
                offset = offsets.get(packet['task_type'], 0)
                choices[offset % len(choices)]['task_ids'].append(packet['task_id'])
                offsets[packet['task_type']] = offset + 1
        for member in members:
            member['task_ids'].sort()
        main = {'session_id': session, 'status': 'available'}
        if old['main'] == main and old['members'] == members:
            return _existing_publication(run, manifest_id, len(assembly.packets)) | {'reused': True}
        raw = safe_child(run, f'manifests/{manifest_id}/run-manifest.json').read_bytes()
        registry = deepcopy(old)
        registry.update(registry_id=str(uuid5(UUID(run_id), sha(raw) + sha(json_bytes(pool)))),
                        main=main, members=members)
        cache = safe_child(run, f'pool-bindings/{manifest_id}.json')
        if cache.exists():
            saved = loads(cache.read_text(encoding='utf-8'))
            _, _, bound, _ = read_state(root, run_id, saved['manifest_id'], rule_catalog)
            if bound.member_registry != registry:
                raise ValueError('pool_binding_conflict')
            current = _existing_publication(run, saved['manifest_id'], len(bound.packets))
            if any(saved[k] != value for k, value in current.items()):
                raise ValueError('pool_binding_changed')
            return current | {'reused': True}
        assembly.member_registry = registry
        assembly.previous_manifest_sha256 = sha(raw)
        published = publish_assembly(root, assembly, bundle, accepted=accepted)
        _write_once(cache, json_bytes(published))
        return published | {'reused': False}
