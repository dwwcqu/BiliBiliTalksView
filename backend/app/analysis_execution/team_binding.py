"""Publish immutable run registries using verified video-level native members."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid5

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_packets.publication import publish_assembly
from app.analysis_results.acceptance import _write_once, load_context, read_state


def team_environment(video, session_id, members, environment):
    from .initialization import load_team

    base = safe_child(video, 'sessions/team')
    if not safe_child(base, 'team.json').exists():
        if safe_child(base, 'intent.json').exists():
            raise ValueError('team_initialization_incomplete')
        return dict(environment)  # Legacy trusted registries have no managed team yet.
    team = load_team(video)
    known = {m['agent_id']: m for m in team['members']}
    if (team['session_id'] != session_id or any(
            m['agent_id'] not in known
            or any(m[k] != known[m['agent_id']][k] for k in ('member_id','role','role_sha256'))
            for m in members)):
        raise ValueError('team_registry_conflict')
    target = team['cli_state_dir']
    if environment.get('CLAUDE_CONFIG_DIR', target) != target:
        raise ValueError('cli_state_dir_conflict')
    return dict(environment, CLAUDE_CONFIG_DIR=target)



def team_definitions(video, member_id):
    from .initialization import load_team

    base = safe_child(video, 'sessions/team')
    if not safe_child(base, 'team.json').exists():
        if safe_child(base, 'intent.json').exists():
            raise ValueError('team_initialization_incomplete')
        return None
    team = load_team(video)
    raw = safe_child(base, 'intent.json').read_bytes()
    if sha(raw) != team['intent_ref']['sha256']:
        raise ValueError('team_hash_mismatch')
    intent = loads(raw.decode('utf-8'))
    matching = [s for s in intent['steps'] if s['member_id'] == member_id]
    if len(matching) != 1:
        raise ValueError('team_member_missing')
    return deepcopy(matching[0]['agents'])


def _registry(assembly, team, manifest_sha):
    old = assembly.member_registry
    by_id = {m['agent_id']: m for m in team['members']}
    if old['main']['status'] != 'uncreated' and (
            old['main']['session_id'] != team['session_id']
            or any(m['agent_id'] not in by_id or m['member_id'] != by_id[m['agent_id']]['member_id']
                   or m['role'] != by_id[m['agent_id']]['role'] for m in old['members'])):
        raise ValueError('team_registry_conflict')
    if any(m['status'] == 'running' for m in old['members']):
        raise ValueError('member_busy')
    previous = {m['agent_id']: m for m in old['members']}
    members = []
    for native in team['members']:
        if native['role_sha256'] != assembly.resources['role_prompt']['sha256']:
            raise ValueError('team_role_refresh_required')
        members.append({k: native[k] for k in ('member_id','agent_id','role','role_sha256')} | {
            'parent_session_id': team['session_id'], 'status': 'available', 'active_task_id': None,
            'task_ids': list(previous.get(native['agent_id'], {}).get('task_ids', [])),
        })
    assigned = {tid for m in members for tid in m['task_ids']}
    offsets = {}
    for packet in sorted(assembly.packets, key=lambda p: p['task_id']):
        choices = [m for m in members if m['role'] == packet['task_type']]
        if not choices:
            raise ValueError('team_role_missing')
        if packet['task_id'] not in assigned:
            offset = offsets.get(packet['task_type'], 0)
            choices[offset % len(choices)]['task_ids'].append(packet['task_id'])
            offsets[packet['task_type']] = offset + 1
    for member in members:
        member['task_ids'].sort()
    registry = deepcopy(old)
    registry.update(
        registry_id=str(uuid5(UUID(assembly.run_id), sha(json_bytes(team)) + manifest_sha)),
        captured_at=datetime.fromisoformat(team['initialized_at']).astimezone(UTC).strftime(
            '%Y-%m-%dT%H:%M:%SZ'),
        main={'session_id':team['session_id'],'status':'available'}, members=members,
    )
    return registry


def _existing_publication(run, manifest_id, task_count):
    path = safe_child(run, f'manifests/{manifest_id}/run-manifest.json')
    return {'status':'offline_prepared', 'run_id':run.name, 'manifest_id':manifest_id,
            'manifest_sha256':sha(path.read_bytes()), 'run_path':str(run),
            'manifest_path':str(path), 'task_count':task_count, 'model_execution_authorized':False}


def bind_team(root, run_id, manifest_id, rule_catalog):
    from .initialization import load_team

    run, _, _, _ = load_context(root, run_id, manifest_id)
    with preparation_lock(safe_child(run, '.dispatch.lock')):
        run, bundle, assembly, catalog = read_state(root, run_id, manifest_id, rule_catalog)
        team = load_team(run.parent.parent)
        if team['video_id'] != bundle.video_id:
            raise ValueError('team_video_mismatch')
        old_raw = safe_child(run, f'manifests/{manifest_id}/run-manifest.json').read_bytes()
        registry = _registry(assembly, team, sha(old_raw))
        if (assembly.member_registry['main'] == registry['main']
                and assembly.member_registry['members'] == registry['members']):
            return _existing_publication(run, manifest_id, len(assembly.packets)) | {'reused':True}
        cache = safe_child(run, f'team-bindings/{manifest_id}.json')
        if cache.exists():
            saved = loads(cache.read_text(encoding='utf-8'))
            _, _, bound, _ = read_state(root, run_id, saved['manifest_id'], rule_catalog)
            if bound.member_registry != registry:
                raise ValueError('team_binding_conflict')
            current = _existing_publication(run, saved['manifest_id'], len(bound.packets))
            if any(saved[k] != v for k, v in current.items()):
                raise ValueError('team_binding_changed')
            return current | {'reused':True}
        assembly.member_registry = registry
        assembly.previous_manifest_sha256 = sha(old_raw)
        published = publish_assembly(root, assembly, bundle, accepted=catalog)
        _write_once(cache, json_bytes(published))
        return published | {'reused':False}
