"""Conservative, source-ordered pagination evidence for partial tail refreshes."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise


@dataclass(frozen=True)
class TailPlan:
    root_id: str
    old_count: int
    new_count: int
    page_size: int
    start_page: int
    last_page: int
    anchor_ids: tuple[str, ...]


class TailMismatch(ValueError):
    """A safe reason code requiring the existing full-thread path."""


def tail_start_page(old_count: int, page_size: int = 20) -> int:
    return max(1, (old_count + page_size - 1) // page_size - 1)


def _stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError('invalid_time')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise TypeError('invalid_time')
    return stamp


def _id(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0


def _identity(row: dict) -> dict:
    identity = {'comment_id': row['comment_id'], 'root_id': row['root_id'],
                'author_uid': row['author']['uid']}
    if not all(_id(value) for value in identity.values()):
        raise ValueError('unknown_identity')
    return identity


def _root(row: dict) -> dict:
    identity = _identity(row)
    if (row['kind'] != 'root' or identity['comment_id'] != identity['root_id']
            or row.get('parent_id') is not None):
        raise ValueError('root_identity')
    return identity


def _signature(row: dict) -> str:
    return hashlib.sha256(json.dumps(row['content'], sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


def _reply(row: dict, root_id: str) -> None:
    identity = _identity(row)
    if (row['kind'] != 'reply' or identity['root_id'] != root_id
            or identity['comment_id'] == root_id or not _id(row.get('parent_id'))
            or row['parent_id'] == row['comment_id']):
        raise ValueError('reply_identity')
    _stamp(row['created_at'])


def _ordered(rows: list[dict], root_id: str) -> list[str]:
    ids, stamps = [], []
    for row in rows:
        _reply(row, root_id)
        ids.append(row['comment_id'])
        stamps.append(_stamp(row['created_at']))
    if len(set(ids)) != len(ids) or any(a > b for a, b in pairwise(stamps)):
        raise ValueError('source_order')
    return ids


def _evidence(root_row: dict, count: int, anchor_ids: list[str], checked_at: str,
              full_at: str) -> dict:
    return {'version': 1, 'source_count': count, 'page_size': 20,
            'anchor_start_page': tail_start_page(count), 'anchor_ids': list(anchor_ids),
            'root_signature': _signature(root_row), 'root_identity': _root(root_row),
            'latest_tail_checked_at': checked_at, 'last_full_checked_at': full_at}


def build_tail_evidence(ordered_replies: list[dict], root_row: dict,
                        source_count: int, checked_at: str) -> dict | None:
    try:
        if type(source_count) is not int or source_count < 0:
            return None
        identity = _root(root_row)
        checked = _stamp(checked_at)
        ids = _ordered(ordered_replies, identity['root_id'])
        if len(ids) != source_count or any(
                _stamp(row['created_at']) > checked for row in ordered_replies):
            return None
        return _evidence(root_row, source_count,
                         ids[(tail_start_page(source_count) - 1) * 20:], checked_at, checked_at)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def validate_tail_evidence(value: object, root_row: dict,
                           old_rows: dict[str, dict], *, snapshot_at: str) -> dict | None:
    try:
        if not isinstance(value, dict):
            return None
        count = value['source_count']
        if (type(value['version']) is not int or value['version'] != 1
                or type(count) is not int or count < 0
                or type(value['page_size']) is not int or value['page_size'] != 20
                or type(value['anchor_start_page']) is not int
                or value['anchor_start_page'] != tail_start_page(count)):
            return None
        identity = _root(root_row)
        stored_identity = value['root_identity']
        if (not isinstance(stored_identity, dict)
                or any(stored_identity.get(key) != val for key, val in identity.items())
                or value['root_signature'] != _signature(root_row)):
            return None
        full, latest = _stamp(value['last_full_checked_at']), _stamp(value['latest_tail_checked_at'])
        if not full <= latest <= _stamp(snapshot_at):
            return None
        ids = value['anchor_ids']
        if (not isinstance(ids, list) or not all(_id(cid) for cid in ids)
                or len(set(ids)) != len(ids)
                or len(ids) != count - (tail_start_page(count) - 1) * 20):
            return None
        replies = []
        for key, row in old_rows.items():
            if row.get('root_id') != identity['root_id']:
                continue
            if key != row['comment_id']:
                return None
            if row['kind'] == 'root':
                if _root(row) != identity:
                    return None
                continue
            _reply(row, identity['root_id'])
            if _stamp(row['created_at']) > latest:
                return None
            replies.append(row)
        if len(replies) != count:
            return None
        anchors = [old_rows[cid] for cid in ids]
        if _ordered(anchors, identity['root_id']) != ids:
            return None
        return _evidence(root_row, count, ids, value['latest_tail_checked_at'],
                         value['last_full_checked_at'])
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def select_tail_plan(state: dict, root_row: dict, old_rows: dict[str, dict],
                     mode: str, new_count: int) -> TailPlan | None:
    if (mode != 'incremental' or type(new_count) is not int
            or state.get('main_observed') is not True or not state.get('main_core')
            or state.get('main_count') != new_count
            or state.get('reply_check_state') != 'complete'
            or any(state.get(key) for key in ('unavailable', 'prior_unavailable', 'tail_disabled'))):
        return None
    value = state.get('tail_evidence')
    if not isinstance(value, dict):
        return None
    evidence = validate_tail_evidence(value, root_row, old_rows,
                                      snapshot_at=value.get('latest_tail_checked_at'))
    if evidence is None:
        return None
    try:
        if (_identity(state['main_core']) != _root(root_row)
                or state['main_core'].get('parent_id') is not None
                or _signature(state['main_core']) != _signature(root_row)):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    old_count = evidence['source_count']
    if old_count <= 60 or new_count <= old_count or state.get('count') != old_count:
        return None
    return TailPlan(root_row['comment_id'], old_count, new_count, 20,
                    evidence['anchor_start_page'], (new_count + 19) // 20,
                    tuple(evidence['anchor_ids']))


def validate_tail_page(plan: TailPlan, payload: dict, page_number: int,
                       old_rows: dict[str, dict], root_row: dict, checked_at: str, *,
                       seen_ids: set[str], previous_created_at: str | None) -> list[dict]:
    """Validate one source page without changing the caller's staged-page state."""
    try:
        if (type(page_number) is not int or not plan.start_page <= page_number <= plan.last_page
                or plan.page_size != 20 or plan.root_id != root_row['comment_id']):
            raise TailMismatch('tail_page_range')
        page = payload['page']
        if any(type(page.get(key)) is not int or page[key] != expected
               for key, expected in [('num', page_number), ('size', 20), ('count', plan.new_count)]):
            raise TailMismatch('tail_page_metadata')
        if (_root(payload['root']) != _root(root_row)
                or _signature(payload['root']) != _signature(root_row)):
            raise TailMismatch('tail_root_changed')
        rows = payload['replies']
        if not isinstance(rows, list) or len(rows) != min(
                20, plan.new_count - (page_number - 1) * 20):
            raise TailMismatch('tail_page_length')
        ids = _ordered(rows, plan.root_id)
        if any(cid in seen_ids for cid in ids):
            raise TailMismatch('tail_duplicate_id')
        offset = (page_number - plan.start_page) * 20
        expected = plan.anchor_ids[offset:offset + len(rows)]
        if tuple(ids[:len(expected)]) != expected:
            raise TailMismatch('tail_anchor_mismatch')
        checked = _stamp(checked_at)
        previous_stamp = _stamp(previous_created_at) if previous_created_at is not None else None
        for index, row in enumerate(rows):
            created = _stamp(row['created_at'])
            if created > checked:
                raise TailMismatch('tail_future_reply')
            if previous_stamp is not None and created < previous_stamp:
                raise TailMismatch('tail_source_order')
            previous_stamp = created
            previous = old_rows.get(row['comment_id'])
            if offset + index < len(plan.anchor_ids):
                if (_identity(previous) != _identity(row)
                        or previous['parent_id'] != row['parent_id']):
                    raise TailMismatch('tail_identity_changed')
            elif previous is not None:
                raise TailMismatch('tail_existing_new_id')
            parent = old_rows.get(row['parent_id'])
            if parent is not None and parent['root_id'] != plan.root_id:
                raise TailMismatch('tail_parent_conflict')
        return deepcopy(rows)
    except TailMismatch:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise TailMismatch('tail_invalid_payload') from exc


def validate_tail_pages(plan: TailPlan, pages: list[dict],
                        old_rows: dict[str, dict], root_row: dict,
                        checked_at: str, prior_evidence: dict) -> tuple[list[dict], dict]:
    try:
        prior = validate_tail_evidence(prior_evidence, root_row, old_rows, snapshot_at=checked_at)
        if prior is None:
            raise TailMismatch('tail_invalid_evidence')
        if (plan.root_id != root_row['comment_id'] or plan.page_size != 20
                or plan.old_count != prior['source_count'] or plan.new_count <= plan.old_count
                or plan.start_page != prior['anchor_start_page']
                or plan.last_page != (plan.new_count + 19) // 20
                or plan.anchor_ids != tuple(prior['anchor_ids'])):
            raise TailMismatch('tail_invalid_plan')
        if len(pages) != plan.last_page - plan.start_page + 1:
            raise TailMismatch('tail_page_range')
        rows, ids, seen = [], [], set()
        previous_created_at = None
        for number, payload in enumerate(pages, plan.start_page):
            page_rows = validate_tail_page(
                plan, payload, number, old_rows, root_row, checked_at,
                seen_ids=seen, previous_created_at=previous_created_at)
            page_ids = [row['comment_id'] for row in page_rows]
            rows.extend(page_rows)
            ids.extend(page_ids)
            seen.update(page_ids)
            if page_rows:
                previous_created_at = page_rows[-1]['created_at']
        if len(ids) - len(plan.anchor_ids) != plan.new_count - plan.old_count:
            raise TailMismatch('tail_new_count')
        offset = (tail_start_page(plan.new_count) - plan.start_page) * 20
        evidence = _evidence(root_row, plan.new_count, ids[offset:], checked_at,
                             prior['last_full_checked_at'])
        return rows, evidence
    except TailMismatch:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise TailMismatch('tail_invalid_payload') from exc
