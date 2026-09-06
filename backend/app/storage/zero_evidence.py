"""Whitelist validation for optional zero-observation storage extensions."""
from uuid import UUID


def public_zero_count(context, video_id, max_roots, coverage, captured_to=None):
    from datetime import datetime

    from app.comment_export.zero_schedule import validate_policy_snapshot

    try:
        if not isinstance(context, dict) or type(context.get('version')) is not int:
            return None
        if context['version'] != 1 or context['video_id'] != video_id:
            return None
        value = context.get('zero_reply_summary')
        snapshot = context['evidence']['refresh'].get('zero_policy_snapshot')
        snapshot = validate_policy_snapshot(snapshot)
        if not isinstance(value, dict) or snapshot is None:
            return None
        if (type(value.get('version')) is not int or value['version'] != 1
                or type(snapshot.get('version')) is not int or snapshot['version'] != 1):
            return None
        work_id = value['work_id']
        if (str(UUID(work_id)) != work_id or snapshot['work_id'] != work_id
                or context.get('job_id') != work_id):
            return None
        if captured_to is not None:
            end = (datetime.fromisoformat(captured_to) if isinstance(captured_to, str)
                   else captured_to)
            if datetime.fromisoformat(snapshot['started_at']) > end:
                return None
        count = value['observed_threads']
        if (type(count) is not int or type(max_roots) is not int
                or not 0 <= count <= max_roots or count > 2**53 - 1
                or (count > 0 and (coverage.get('status') != 'partial'
                                  or snapshot['zero_policy'] != 'observe'))):
            return None
        return count
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def filter_zero_extensions(metadata: dict, rows: list[dict], *, strict=False, work_id=None):
    """Return sanitized optional fields; strict publication checks actual zero decisions."""
    from collections import Counter
    from copy import deepcopy
    from datetime import datetime

    from app.comment_export.zero_reply import merge_history, validate_zero_evidence
    from app.comment_export.zero_schedule import validate_policy_snapshot, validate_schedule

    cleaned = deepcopy(metadata)
    refresh = cleaned.get('_refresh', {})
    row_map = {row['comment_id']: row for row in rows}
    reply_counts = Counter(row['root_id'] for row in rows if row['kind'] == 'reply')
    original_refresh = metadata.get('_refresh', {})
    observed_ids = set(original_refresh.get('observed_ids', []))
    observed_main_ids = set(original_refresh.get('observed_main_ids', []))
    snapshot = validate_policy_snapshot(original_refresh.get('zero_policy_snapshot'))
    if snapshot is not None:
        try:
            if (snapshot['baseline_binding'] != original_refresh.get('baseline_digest')
                    or snapshot['range_mode'] != original_refresh.get('mode')
                    or datetime.fromisoformat(snapshot['started_at'])
                    > datetime.fromisoformat(metadata['captured_to'])):
                snapshot = None
        except (KeyError, TypeError, ValueError):
            snapshot = None
    if snapshot is not None and work_id is not None and snapshot['work_id'] != work_id:
        if strict:
            raise ValueError('zero_work_mismatch')
        snapshot = None
    refresh.pop('zero_policy_snapshot', None)
    refresh.pop('zero_reply_schedule', None)
    refresh_extensions = {}
    if snapshot is not None:
        refresh['zero_policy_snapshot'] = snapshot
        refresh_extensions['zero_policy_snapshot'] = snapshot
        if 'baseline_digest' in original_refresh:
            refresh_extensions['baseline_digest'] = original_refresh['baseline_digest']
    cleaned['_refresh'] = refresh
    thread_extensions, zero_count = {}, 0
    for root, state in cleaned.get('_threads', {}).items():
        incoming = metadata['_threads'][root]
        extensions = {}
        history = incoming.get('zero_reply_history')
        if history is not None:
            history = merge_history(history, is_new=False,
                nonzero=bool(reply_counts[root]) or any(
                    type(incoming.get(k)) is int and incoming[k] > 0
                    for k in ('count', 'checked_count', 'main_count')),
                unavailable=bool(incoming.get('unavailable') or incoming.get('prior_unavailable')))
            state['zero_reply_history'] = history
            extensions['zero_reply_history'] = history
        state.pop('zero_reply_evidence', None)
        main_count = incoming.get('main_count')
        if type(main_count) is int and main_count >= 0:
            extensions['main_count'] = main_count
        else:
            state.pop('main_count', None)
        evidence = None
        eligible_state = (
            snapshot is not None and snapshot['zero_policy'] == 'observe'
            and state.get('pagination_status') == 'partial'
            and state.get('reply_verification') == 'not_checked'
            and type(state.get('count')) is int and state['count'] == 0
            and not state.get('unavailable')
            and root in observed_ids
            and (not strict or incoming.get('zero_completed') is True)
            and not incoming.get('skip_refresh') and not incoming.get('tail_completed'))
        if eligible_state:
            evidence = validate_zero_evidence(incoming.get('zero_reply_evidence'),
                work_id=snapshot['work_id'], video_id=metadata['video_id'],
                root_row=row_map.get(root), history=history,
                stored_replies=reply_counts[root],
                observed_main_ids=observed_main_ids,
                snapshot_at=metadata['captured_to'], started_at=snapshot['started_at'])
        if strict and incoming.get('zero_completed') is True and evidence is None:
            raise ValueError('invalid_zero_decision')
        if evidence is not None:
            state['zero_reply_evidence'] = evidence
            extensions['zero_reply_evidence'] = evidence
            if strict:
                zero_count += 1
        thread_extensions[root] = extensions
    proof = validate_schedule(original_refresh.get('zero_reply_schedule'), cleaned, rows)
    if proof is not None:
        refresh_extensions['zero_reply_schedule'] = proof
    elif strict and original_refresh.get('zero_reply_schedule') is not None:
        raise ValueError('invalid_zero_schedule')
    summary = ({'version': 1, 'work_id': snapshot['work_id'], 'observed_threads': zero_count}
               if strict and snapshot is not None else None)
    return refresh_extensions, thread_extensions, summary


def check_zero_history_transition(conn, previous_state_ids, incoming_context):
    """An aware writer cannot erase negative history while replacing a visible state."""
    from sqlalchemy import select

    from .codec import decode_json
    from .errors import StorageError
    from .schema import discussion_states

    incoming = incoming_context.get('evidence', {}).get('threads', {})
    for value in conn.execute(select(discussion_states.c.refresh_context).where(
            discussion_states.c.state_id.in_([sid for sid in previous_state_ids if sid]))).scalars():
        if value is None:
            continue
        context = decode_json(value)
        for root, state in context.get('evidence', {}).get('threads', {}).items():
            history = state.get('zero_reply_history')
            if not isinstance(history, dict):
                continue
            new_history = incoming.get(root, {}).get('zero_reply_history')
            new_history = new_history if isinstance(new_history, dict) else {}
            for key in ('ever_nonzero_observed', 'ever_source_unavailable'):
                if history.get(key) is True and new_history.get(key) is not True:
                    raise StorageError('zero_history_regression')
