"""Conservative main-list observations; these never establish detail verification."""

from copy import deepcopy
from datetime import datetime

from .normalization import external_id

_QUALIFICATIONS = ('identity_valid', 'rcount_zero', 'aux_zero', 'preview_empty')
_HISTORY_FLAGS = ('ever_nonzero_observed', 'ever_source_unavailable')
_OBSERVATION_FIELDS = (
    'version', 'video_id', 'root_id', 'comment_type', 'observed_at', 'source',
    'root_identity', *_QUALIFICATIONS, 'conflict', 'nonzero_observed',
)


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def _zero(value: object) -> bool:
    return type(value) is int and value == 0


def _raw_id(raw: dict, name: str, *, zero: bool = False) -> str | None:
    values = [raw[key] for key in (name + '_str', name) if key in raw]
    if not values:
        return None
    parsed = [str(value) if zero and type(value) in (int, str) and str(value) == '0'
              else external_id(value) for value in values]
    return parsed[0] if parsed[0] is not None and all(x == parsed[0] for x in parsed) else None


def observe_zero(raw: dict, source: dict, observed_at: str) -> dict:
    """Observe one successful, adapter-validated main-list item without changing it."""
    root = _raw_id(raw, 'rpid')
    oid = external_id(source.get('oid'))
    comment_type = source.get('comment_type')
    identity = (root is not None and oid is not None and _raw_id(raw, 'oid') == oid
                and _raw_id(raw, 'root', zero=True) == '0'
                and type(comment_type) is int and comment_type > 0
                and type(raw.get('type')) is int and raw['type'] == comment_type)
    preview = raw.get('replies')
    member = raw.get('member') if isinstance(raw.get('member'), dict) else {}
    result = {
        'version': 1, 'video_id': f'bilibili:video:{oid}' if oid else None,
        'root_id': root, 'comment_type': comment_type, 'observed_at': observed_at,
        'source': 'main_list', 'root_identity': {
            'comment_id': root, 'root_id': root,
            'author_uid': external_id(member.get('mid_str', member.get('mid'))),
        },
        'identity_valid': identity, 'rcount_zero': _zero(raw.get('rcount')),
        'aux_zero': 'count' not in raw or _zero(raw['count']),
        'preview_empty': preview is None or (type(preview) is list and not preview),
        'nonzero_observed': any(type(raw.get(key)) is int and raw[key] > 0
                                for key in ('rcount', 'count'))
                            or (type(preview) is list and bool(preview)),
    }
    result['conflict'] = not all(result[key] for key in _QUALIFICATIONS)
    return result


def _observation_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    root = value.get('root_id')
    identity = value.get('root_identity')
    video = value.get('video_id')
    return (type(value.get('version')) is int and value['version'] == 1
            and external_id(root) == root and root is not None
            and isinstance(video, str) and video.startswith('bilibili:video:')
            and external_id(video.removeprefix('bilibili:video:')) is not None
            and type(value.get('comment_type')) is int and value['comment_type'] > 0
            and value.get('source') == 'main_list' and _stamp(value.get('observed_at')) is not None
            and isinstance(identity, dict) and set(identity) == {
                'comment_id', 'root_id', 'author_uid'}
            and identity['comment_id'] == root and identity['root_id'] == root
            and (identity['author_uid'] is None
                 or (isinstance(identity['author_uid'], str)
                     and external_id(identity['author_uid']) == identity['author_uid']))
            and all(value.get(key) is True for key in _QUALIFICATIONS)
            and value.get('conflict') is False and value.get('nonzero_observed') is False)


def merge_observations(previous: dict | None, incoming: dict) -> dict:
    """Keep all disqualifications for a root throughout the current scan."""
    if previous is None:
        result = deepcopy(incoming)
        result['conflict'] = not _observation_valid(incoming)
        return result
    result = deepcopy(previous)
    same_identity = all(previous.get(key) == incoming.get(key) for key in (
        'video_id', 'root_id', 'root_identity', 'comment_type', 'source'))
    result['conflict'] = (not _observation_valid(previous) or not _observation_valid(incoming)
                          or not same_identity)
    for key in _QUALIFICATIONS:
        result[key] = previous.get(key) is True and incoming.get(key) is True
    result['nonzero_observed'] = (previous.get('nonzero_observed') is True
                                  or incoming.get('nonzero_observed') is True)
    return result


def merge_history(previous: object, *, is_new: bool, nonzero: bool, unavailable: bool) -> dict:
    """Only a genuinely new root starts a known history; counterevidence never clears."""
    prior = previous if isinstance(previous, dict) else {}
    valid = (type(prior.get('version')) is int and prior['version'] == 1
             and type(prior.get('known')) is bool
             and all(type(prior.get(key)) is bool for key in _HISTORY_FLAGS))
    return {
        'version': 1,
        'known': prior['known'] if valid else previous is None and is_new is True,
        'ever_nonzero_observed': prior.get('ever_nonzero_observed') is True or nonzero is True,
        'ever_source_unavailable': prior.get('ever_source_unavailable') is True
                                   or unavailable is True,
    }


def can_omit(observation: dict, history: dict, *, stored_replies: int, policy: str) -> bool:
    """Main-list evidence permits omission only under the frozen observe policy."""
    normalized = merge_history(history, is_new=False, nonzero=False, unavailable=False)
    return (policy == 'observe' and _zero(stored_replies) and _observation_valid(observation)
            and normalized['known'] is True
            and all(normalized[key] is False for key in _HISTORY_FLAGS))


def _matches_root(value: dict, root_row: object) -> bool:
    if not isinstance(root_row, dict) or not isinstance(root_row.get('author'), dict):
        return False
    return (root_row.get('kind') == 'root' and root_row.get('video_id') == value.get('video_id')
            and value.get('root_identity') == {
                'comment_id': root_row.get('comment_id'), 'root_id': root_row.get('root_id'),
                'author_uid': root_row['author'].get('uid'),
            })


def build_zero_evidence(observation: dict, history: dict, root_row: dict,
                        work_id: str) -> dict | None:
    """Build internal evidence after the caller selected omission with no stored replies."""
    if (not isinstance(work_id, str) or not work_id
            or not can_omit(observation, history, stored_replies=0, policy='observe')
            or not _matches_root(observation, root_row)):
        return None
    return {**{key: deepcopy(observation[key]) for key in _OBSERVATION_FIELDS}, 'work_id': work_id}


def validate_zero_evidence(value: object, *, work_id: str, video_id: str, root_row: dict,
                           history: dict, stored_replies: int, observed_main_ids,
                           snapshot_at: str, started_at: str) -> dict | None:
    """Whitelist evidence bound to this work, its observed root and frozen time range."""
    if not isinstance(value, dict):
        return None
    observed = _stamp(value.get('observed_at'))
    started, snapshot = _stamp(started_at), _stamp(snapshot_at)
    if (not all((observed, started, snapshot)) or not started <= observed <= snapshot
            or value.get('work_id') != work_id or value.get('video_id') != video_id
            or not isinstance(observed_main_ids, (list, tuple, set))
            or value.get('root_id') not in observed_main_ids
            or not can_omit(value, history, stored_replies=stored_replies, policy='observe')):
        return None
    return build_zero_evidence(value, history, root_row, work_id)
