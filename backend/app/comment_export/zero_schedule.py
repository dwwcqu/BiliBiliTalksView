"""Frozen zero-observation deadlines and data-bound completion evidence."""

import hashlib
import json
import math
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta

from .tail import validate_tail_evidence


def _stamp(value):
    if not isinstance(value, str):
        raise TypeError('invalid_time')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise TypeError('invalid_time')
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


def _interval(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError('invalid_full_interval')
    return timedelta(hours=value)


def decide_policy(requested_mode: str, now: str, interval_hours: int, baseline: dict | None,
                  *, work_id: str, baseline_binding: str | None) -> dict:
    """Baseline may supply transient `_zero_schedule_rows` for proof verification."""
    stamp, interval = _stamp(now), _interval(interval_hours)
    if requested_mode not in {'auto', 'full'} or not isinstance(work_id, str) or not work_id:
        raise ValueError('invalid_zero_policy')
    anchors = []
    if baseline is not None:
        prior = baseline.get('_refresh', {})
        proof = validate_schedule(prior.get('zero_reply_schedule'), baseline,
                                  baseline.get('_zero_schedule_rows', []))
        if proof is not None and _stamp(proof['scan_completed_at']) <= stamp:
            anchor = _stamp(proof['verification_anchor_at'])
            anchors.append((min(_stamp(proof['verify_due_at']), anchor + interval), anchor))
        try:
            full = _stamp(prior.get('last_full_scan_completed_at'))
            if prior.get('full_scan_incomplete') is False and full <= _stamp(
                    baseline['captured_to']) <= stamp:
                anchors.append((full + interval, full))
        except (KeyError, TypeError, ValueError):
            pass
    due, anchor = min(anchors) if anchors else (stamp + interval, stamp)
    observe = requested_mode == 'auto' and (baseline is None or bool(anchors) and stamp < due)
    return {'version': 1, 'work_id': work_id, 'requested_mode': requested_mode,
            'range_mode': 'incremental' if observe and baseline is not None else 'full',
            'zero_policy': 'observe' if observe else 'verify', 'started_at': now,
            'interval_hours': interval_hours, 'verification_anchor_at': anchor.isoformat(),
            'verify_due_at': due.isoformat(), 'baseline_binding': baseline_binding}


def _snapshot(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or not isinstance(value.get('work_id'), str)
            or not value['work_id'] or value.get('requested_mode') not in {'auto', 'full'}
            or value.get('range_mode') not in {'full', 'incremental'}
            or value.get('zero_policy') not in {'observe', 'verify'}):
        raise ValueError('invalid_snapshot')
    start = _stamp(value['started_at'])
    anchor, due = _stamp(value['verification_anchor_at']), _stamp(value['verify_due_at'])
    if not anchor <= start or not anchor < due <= anchor + _interval(value['interval_hours']):
        raise ValueError('invalid_deadline')
    if value['zero_policy'] == 'verify' and value['range_mode'] != 'full':
        raise ValueError('invalid_verify_range')
    if (value['requested_mode'] == 'full' and value['zero_policy'] != 'verify'
            or value['zero_policy'] == 'observe' and start >= due):
        raise ValueError('inconsistent_snapshot')
    return start


def validate_policy_snapshot(value: object) -> dict | None:
    try:
        _snapshot(value)
        keys = ('version', 'work_id', 'requested_mode', 'range_mode', 'zero_policy',
                'started_at', 'interval_hours', 'verification_anchor_at', 'verify_due_at',
                'baseline_binding')
        if value['baseline_binding'] is not None and not isinstance(value['baseline_binding'], str):
            return None
        return {key: deepcopy(value[key]) for key in keys}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _action(state):
    if state.get('zero_completed') is True:
        return 'zero'
    if state.get('skip_refresh') is True:
        return 'skip'
    if state.get('tail_completed') is True:
        return 'tail'
    if state.get('unavailable'):
        return 'unavailable'
    return 'full'


def _validate_action(action, state, root_row, row_map, replies, snapshot, observed, completed):
    start = _snapshot(snapshot)
    root = root_row['comment_id']
    if (root_row.get('kind') != 'root' or root_row.get('root_id') != root
            or root_row.get('parent_id') is not None):
        return False
    if action == 'zero':
        from .zero_reply import validate_zero_evidence
        return (snapshot['zero_policy'] == 'observe'
                and state.get('pagination_status') == 'partial'
                and state.get('reply_verification') != 'checked_now'
                and validate_zero_evidence(state.get('zero_reply_evidence'),
                    work_id=snapshot['work_id'], video_id=root_row['video_id'],
                    root_row=root_row, history=state.get('zero_reply_history'),
                    stored_replies=len(replies), observed_main_ids=observed,
                    snapshot_at=completed, started_at=snapshot['started_at']) is not None)
    if action == 'unavailable' or (action == 'skip' and state.get('unavailable')):
        evidence = state.get('unavailable', {})
        at = _stamp(evidence.get('observed_at'))
        return (state.get('reply_verification') == 'source_unavailable'
                and evidence.get('http_status') == 200
                and type(evidence.get('api_code')) is int
                and evidence['api_code'] in {12006, 12022}
                and type(evidence.get('page')) is int and evidence['page'] > 0
                and at <= _stamp(completed)
                and (at >= start if action == 'unavailable' else
                     snapshot['range_mode'] == 'incremental' and root not in observed))
    if action == 'tail':
        evidence = validate_tail_evidence(state.get('tail_evidence'), root_row, row_map,
                                          snapshot_at=completed)
        return (snapshot['range_mode'] == 'incremental' and evidence is not None
                and root in observed
                and _stamp(evidence['latest_tail_checked_at']) >= start
                and state.get('reply_verification') == 'reused_unverified')
    checked = state.get('checked_count')
    if (state.get('reply_check_state') != 'complete' or type(checked) is not int or checked < 0
            or state.get('checked_root') != _digest(root_row['content'])):
        return False
    at = _stamp(state.get('last_reply_checked_at'))
    if at > _stamp(completed):
        return False
    if action == 'full':
        return (state.get('reply_verification') == 'checked_now' and at >= start
                and type(state.get('count')) is int and state['count'] == checked
                and checked <= len(replies)
                and _stamp(state.get('last_complete_at')) == at)
    if action == 'skip':
        return (snapshot['range_mode'] == 'incremental'
                and state.get('reply_verification') == 'reused_unverified'
                and at <= start and (root not in observed or (
                    type(state.get('main_count')) is int
                    and state['main_count'] == _latest_count(state, root_row, row_map, completed))))
    return False


def _latest_count(state, root_row, row_map, completed):
    evidence = validate_tail_evidence(state.get('tail_evidence'), root_row, row_map,
                                      snapshot_at=completed)
    return evidence['source_count'] if evidence is not None else state['checked_count']


def _binding(metadata, rows, snapshot, actions, observed):
    # Deliberately exclude export envelope fields and temporary collection state.
    normalized = [{key: value for key, value in row.items()
                   if key not in {'schema_version', 'export_id'}} for row in rows]
    normalized.sort(key=lambda row: row['comment_id'])
    evidence_keys = ('pagination_status', 'reply_check_state', 'reply_verification', 'count',
                     'checked_count', 'checked_root', 'last_complete_at', 'last_reply_checked_at',
                     'unavailable', 'tail_evidence', 'zero_reply_history', 'zero_reply_evidence')
    states = {root: {key: state[key] for key in evidence_keys if key in state}
              for root, state in metadata['_threads'].items()}
    for root, state in metadata['_threads'].items():
        if type(state.get('main_count')) is int and state['main_count'] >= 0:
            states[root]['main_count'] = state['main_count']
    return _digest({'video_id': metadata['video_id'], 'snapshot': snapshot,
                    'actions': actions, 'observed_main_ids': observed,
                    'observed_ids': sorted(set(metadata['_refresh'].get('observed_ids', []))),
                    'rows': normalized, 'states': states})


def _verify_inputs(snapshot, metadata, rows, actions, observed, completed):
    start = _snapshot(snapshot)
    if not start <= _stamp(completed) <= _stamp(metadata['captured_to']):
        return False
    if (metadata['_refresh'].get('zero_policy_snapshot') != snapshot
            or metadata['_refresh'].get('baseline_digest') != snapshot.get('baseline_binding')):
        return False
    row_map = {row['comment_id']: row for row in rows}
    roots = set(metadata['_threads'])
    if (len(row_map) != len(rows) or set(actions) != roots
            or {row['root_id'] for row in rows} != roots
            or not isinstance(observed, list) or len(set(observed)) != len(observed)
            or not set(observed) <= roots):
        return False
    observed_ids = metadata['_refresh'].get('observed_ids', [])
    if (not isinstance(observed_ids, list) or len(set(observed_ids)) != len(observed_ids)
            or not set(observed_ids) <= set(row_map)):
        return False
    observed_set, observed_main = set(observed_ids), set(observed)
    replies_by_root, rows_by_root = defaultdict(list), defaultdict(dict)
    observed_reply_counts = Counter()
    for row in rows:
        root = row['root_id']
        rows_by_root[root][row['comment_id']] = row
        if row['kind'] == 'reply':
            replies_by_root[root].append(row)
            if row['comment_id'] in observed_set:
                observed_reply_counts[root] += 1
    for root, action in actions.items():
        if action == 'full' and (root not in observed_set
                or observed_reply_counts[root] != metadata['_threads'][root].get('checked_count')):
            return False
        root_row = row_map.get(root)
        if (not root_row or root_row.get('video_id') != metadata['video_id']
                or not _validate_action(action, metadata['_threads'][root], root_row,
                                         rows_by_root[root], replies_by_root[root],
                                         snapshot, observed_main, completed)):
            return False
    return all(row.get('video_id') == metadata['video_id'] for row in rows)


def complete_schedule(snapshot: dict, metadata: dict, rows: list[dict], progress: dict
                      ) -> dict | None:
    """Completion requires persisted per-root evidence in addition to loop flags."""
    try:
        if progress.get('finished') is not True or progress.get('main_done') is not True:
            return None
        if progress.get('blocked') or progress.get('stopped_reason'):
            return None
        completed = metadata['captured_to']
        actions = {root: _action(state) for root, state in metadata['_threads'].items()}
        observed = sorted(set(metadata['_refresh']['observed_main_ids']))
        if not _verify_inputs(snapshot, metadata, rows, actions, observed, completed):
            return None
        full = snapshot['range_mode'] == 'full' and all(
            action in {'full', 'unavailable'} for action in actions.values())
        anchor = completed if full else snapshot['verification_anchor_at']
        due = (_stamp(completed) + _interval(snapshot['interval_hours'])).isoformat() if full \
            else snapshot['verify_due_at']
        return {'version': 1, 'work_id': snapshot['work_id'], 'video_id': metadata['video_id'],
                'baseline_binding': snapshot['baseline_binding'], 'scan_completed_at': completed,
                'verification_anchor_at': anchor, 'verify_interval_hours': snapshot['interval_hours'],
                'verify_due_at': due, 'scan_policy': snapshot['zero_policy'], 'main_done': True,
                'full_details_completed': full, 'actions': actions, 'observed_main_ids': observed,
                'binding': _binding(metadata, rows, snapshot, actions, observed)}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def validate_schedule(value: object, metadata: dict, rows: list[dict]) -> dict | None:
    try:
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
            return None
        snapshot = metadata['_refresh']['zero_policy_snapshot']
        if not _verify_inputs(snapshot, metadata, rows, value['actions'],
                              value['observed_main_ids'], value['scan_completed_at']):
            return None
        # Reconstruct using the persisted action set, without requiring temporary flags.
        staged = deepcopy(metadata)
        staged['captured_to'] = value['scan_completed_at']
        staged['_refresh']['observed_main_ids'] = value['observed_main_ids']
        for root, action in value['actions'].items():
            state = staged['_threads'][root]
            for key in ('zero_completed', 'skip_refresh', 'tail_completed'):
                state.pop(key, None)
            flag = {'zero': 'zero_completed', 'skip': 'skip_refresh', 'tail': 'tail_completed'}
            if action in flag:
                state[flag[action]] = True
        expected = complete_schedule(snapshot, staged, rows, {'finished': True, 'main_done': True})
        return deepcopy(expected) if expected == value else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
